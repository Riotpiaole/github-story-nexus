import logging
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate

from config import get_llm_with_tools, settings
from state import AgentState
from tools.executor import execute_python_tests
from tools.langchain_tools import get_code_search_tools

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent
_MAX_LOOP_ITERATIONS = 10

CODE_GENERATION_TOKEN_LIMIT = 5000


def _load_prompt(filename: str, **kwargs) -> str:
    raw = (_PROMPTS_DIR / filename).read_text()
    return raw.format(**kwargs) if kwargs else raw




_CODE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _load_prompt(
        "coder_prompt.md",
        token_limit=CODE_GENERATION_TOKEN_LIMIT,
        constraints="output ONLY valid, executable code — no markdown fences, no explanations",
    )),
    ("user", (
        "GitHub Issue:\n{issue}\n\n"
        "Previous test failure (empty if first attempt):\n{test_feedback}"
    )),
])


_TEST_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are an expert software engineer writing pytest tests.\n"
        "Given a solution file (imported as `from solution import *`), write a pytest test file.\n"
        "You can search the codebase to understand testing patterns if needed.\n"
        "Cover happy paths and edge cases.\n"
        "After searching (if needed), output ONLY valid pytest code. No markdown fences, no explanations."
    )),
    ("user", (
        "GitHub Issue (what the code should do):\n{issue}\n\n"
        "Solution code:\n{code}"
    )),
])


def _run_agentic_loop(
    prompt: ChatPromptTemplate,
    variables: dict,
    repo_path: str,
) -> str:
    llm_with_tools = get_llm_with_tools()
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
            if fn is None:
                result = f"Unknown tool: {call['name']}"
            else:
                result = fn.invoke({**call["args"], "repo_path": repo_path})
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))

    log.warning("Agentic loop reached max iterations (%d).", _MAX_LOOP_ITERATIONS)
    for msg in reversed(messages):
        if hasattr(msg, "content") and isinstance(msg.content, str) and msg.content:
            return msg.content
    return ""



def code_solution(state: AgentState) -> dict:
    """Generates Python solution code using Claude.

    Takes the GitHub issue and any prior test failures, returns executable Python code.
    Uses agentic loop to search codebase with tools if needed.
    """
    log.info("Generating solution with Claude (attempt %d/%d)...", state["retry_count"] + 1, settings.max_retries)
    feedback = state.get("test_results") or "None — this is the first attempt."


    code = _run_agentic_loop(
        _CODE_PROMPT,
        {
            "summary": state["project_context"],
            "issue": state["issue_details"],
            "test_feedback": feedback,
        },
        state["local_repo_path"],
    )
    return {"code_snippet": code}


def generate_tests(state: AgentState) -> dict:
    """Generates pytest test file using Claude.

    Takes the issue description and generated solution code, returns pytest test code.
    """
    log.info("Generating pytest tests with Claude...")

    test_code = _run_agentic_loop(
        _TEST_PROMPT,
        {
            "issue": state["issue_details"],
            "code": state["code_snippet"],
        },
        state["local_repo_path"],
    )
    return {"test_code": test_code}


def test_code(state: AgentState) -> dict:
    """Executes solution and test code in isolated Docker container.

    Returns 'success' if tests pass, 'retry' if tests fail and retries remain.
    """
    log.info("Running tests in Docker sandbox...")
    test_run = execute_python_tests(state["code_snippet"], state["test_code"])
    if test_run["success"]:
        log.info("Tests passed.")
        return {"test_results": "passed", "status": "success"}
    else:
        log.warning("Tests failed (attempt %d/%d).", state["retry_count"] + 1, settings.max_retries)
        return {
            "test_results": f"Execution failed:\n{test_run['output']}",
            "retry_count": state["retry_count"] + 1,
            "status": "retry",
        }
