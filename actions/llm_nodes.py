import logging
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate

from config import get_bounded_llm, get_bounded_llm_with_tools, llm, settings
from state import AgentState
from tools.executor import execute_tests
from tools.langchain_tools import get_code_search_tools

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent
_MAX_LOOP_ITERATIONS = 10


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text()


_PLAN_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _load_prompt("planner_prompt.md")),
    ("user", "GitHub Issue:\n{issue}\n\nProject Context:\n{context}"),
])

_CODE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _load_prompt("coder_prompt.md")),
    ("user", (
        "Implementation Plan:\n{plan}\n\n"
        "GitHub Issue:\n{issue}\n\n"
        "Project Context:\n{context}\n\n"
        "Tester feedback from previous attempt (empty on first attempt):\n{tester_feedback}"
    )),
])

_TESTER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _load_prompt("tester_prompt.md")),
    ("user", (
        "Implementation Plan:\n{plan}\n\n"
        "Solution code:\n{code}\n\n"
        "Unit tests:\n{tests}\n\n"
        "Test runner: {test_runner}\n\n"
        "Execution output:\n{output}"
    )),
])


def _run_agentic_loop(
    prompt: ChatPromptTemplate,
    variables: dict,
    repo_path: str,
) -> str:
    """Runs an agentic LLM loop with codebase search tools.

    Iterates up to _MAX_LOOP_ITERATIONS, executing any tool calls the LLM makes,
    then returns the final text response.
    """
    llm_with_tools = get_bounded_llm_with_tools()
    tools = {t.name: t for t in get_code_search_tools()}
    messages = prompt.format_messages(**variables)

    for iteration in range(_MAX_LOOP_ITERATIONS):
        response = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            return response.content

        log.info("Loop iteration %d: executing %d tool call(s).", iteration + 1, len(response.tool_calls))
        for call in response.tool_calls:
            fn = tools.get(call["name"])
            result = fn.invoke({**call["args"], "repo_path": repo_path}) if fn else f"Unknown tool: {call['name']}"
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))

    log.warning("Agentic loop reached max iterations (%d).", _MAX_LOOP_ITERATIONS)
    for msg in reversed(messages):
        if hasattr(msg, "content") and isinstance(msg.content, str) and msg.content:
            return msg.content
    return ""


def _parse_coder_output(raw: str) -> tuple[str, str]:
    """Splits the coder's two-section output into (solution_code, test_code)."""
    solution, _, rest = raw.partition("### TESTS")
    solution = solution.replace("### SOLUTION", "").strip()
    return solution, rest.strip()


def _parse_tester_verdict(raw: str) -> str:
    """Extracts APPROVED or NEEDS_WORK from the first line of tester output."""
    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    if "APPROVED" in first_line.upper():
        return "APPROVED"
    return "NEEDS_WORK"


def plan_solution(state: AgentState) -> dict:
    """Generates a structured implementation plan from the issue and cached project context.

    Uses a plain LLM call (no tools) — project_context is already a complete repo summary.
    """
    log.info("Planning solution for issue #%d...", state["issue_number"])
    messages = _PLAN_PROMPT.format_messages(
        issue=state["issue_details"],
        context=state["project_context"],
    )
    response = llm.invoke(messages)
    return {"implementation_plan": response.content}


def code_solution(state: AgentState) -> dict:
    """Generates solution code and unit tests using an agentic loop.

    On retry, receives structured tester feedback to guide revision of both
    the solution and the tests.
    """
    log.info("Generating solution (attempt %d/%d)...", state["retry_count"] + 1, settings.max_retries)
    raw = _run_agentic_loop(
        _CODE_PROMPT,
        {
            "plan": state.get("implementation_plan", ""),
            "issue": state["issue_details"],
            "context": state["project_context"],
            "tester_feedback": state.get("tester_feedback") or "None — first attempt.",
        },
        state["local_repo_path"],
    )
    solution, tests = _parse_coder_output(raw)
    return {"code_snippet": solution, "test_code": tests}


def test_code(state: AgentState) -> dict:
    """Executes solution and unit tests inside an isolated Docker container.

    Dispatches the correct test command based on skills.test_runner.
    Returns 'success' status if tests pass, 'retry' if they fail.
    """
    log.info("Running tests in Docker sandbox...")
    test_runner = state.get("skills", {}).get("test_runner", "pytest")
    result = execute_tests(state["code_snippet"], state["test_code"], test_runner)
    if result["success"]:
        log.info("Tests passed.")
        return {"test_results": result["output"], "status": "success"}
    log.warning("Tests failed (attempt %d/%d).", state["retry_count"] + 1, settings.max_retries)
    return {
        "test_results": f"Execution failed:\n{result['output']}",
        "status": "retry",
    }


def tester_review(state: AgentState) -> dict:
    """Analyzes test execution failure and produces structured feedback for the coder.

    Checks for early termination: if the solution already satisfies the issue
    requirements (e.g. only the tests were wrong), sets status to 'tester_approved'
    to skip further coder iterations and go straight to PR creation.

    Increments retry_count so each full coder→tester cycle counts as one attempt.
    """
    log.info("Tester analyzing failure (attempt %d/%d)...", state["retry_count"] + 1, settings.max_retries)
    test_runner = state.get("skills", {}).get("test_runner", "pytest")
    messages = _TESTER_PROMPT.format_messages(
        plan=state.get("implementation_plan", ""),
        code=state["code_snippet"],
        tests=state["test_code"],
        test_runner=test_runner,
        output=state.get("test_results", ""),
    )
    response = get_bounded_llm().invoke(messages)
    verdict = _parse_tester_verdict(response.content)

    if verdict == "APPROVED":
        log.info("Tester approved solution early — routing to PR creation.")
        return {
            "tester_feedback": response.content,
            "status": "tester_approved",
            "retry_count": state["retry_count"] + 1,
        }

    log.info("Tester verdict: NEEDS_WORK.")
    return {
        "tester_feedback": response.content,
        "retry_count": state["retry_count"] + 1,
    }
