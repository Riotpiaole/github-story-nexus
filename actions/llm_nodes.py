import json
import logging
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate

from cache import get_local_cache, make_cache_key
from cache._redis import redis_get as _node_redis_get
from cache._redis import redis_set as _node_redis_set
from config import get_bounded_coder_llm_with_tools, get_bounded_llm, llm, settings
from state import AgentState
from tools.executor import execute_tests
from tools.langchain_tools import get_coder_tools

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent
_MAX_LOOP_ITERATIONS = 10


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text()


_PLAN_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _load_prompt("planner_prompt.md")),
    ("user", (
        "GitHub Issue:\n{issue}\n\n"
        "Project Context:\n{context}\n\n"
        "Previous attempt feedback (empty on first attempt):\n{tester_feedback}"
    )),
])

_STEP_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _load_prompt("coder_prompt.md")),
    ("user", (
        "Full Implementation Plan:\n{plan}\n\n"
        "Current Step ({step_num} of {total_steps}):\n{step}\n\n"
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


_FILE_WRITE_TOOLS = {"write_file", "str_replace_in_file"}


def _node_cache_get(key_str: str) -> dict | None:
    local = get_local_cache()
    key, compressed = make_cache_key(key_str)
    raw = local.get(key, compressed)
    if raw is not None:
        log.debug("node cache L1 hit key=%s", key)
        return json.loads(raw)
    raw = _node_redis_get(settings.redis_url, key)
    if raw is not None:
        log.debug("node cache L2 hit key=%s — backfilling L1", key)
        local.set(key, compressed, raw)
        return json.loads(raw)
    return None


def _node_cache_set(key_str: str, value: dict) -> None:
    local = get_local_cache()
    key, compressed = make_cache_key(key_str)
    raw = json.dumps(value)
    local.set(key, compressed, raw)
    _node_redis_set(settings.redis_url, key, raw)


def _run_react_loop(
    prompt: ChatPromptTemplate,
    variables: dict,
    repo_path: str,
) -> tuple[list[str], str]:
    """Runs a ReAct LLM loop: the agent reads/edits repo files directly via tools.

    Tracks which files are written or modified during the loop. When the agent
    makes no further tool calls its final message must contain ### TESTS.

    Returns:
        (modified_files, test_code) — repo-relative paths that were written,
        and the unit test code extracted from the agent's final response.
    """
    llm_with_tools = get_bounded_coder_llm_with_tools()
    tools = {t.name: t for t in get_coder_tools()}
    messages = prompt.format_messages(**variables)
    modified_files: list[str] = []

    for iteration in range(_MAX_LOOP_ITERATIONS):
        response = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            _, _, rest = response.content.partition("### TESTS")
            return modified_files, rest.strip()

        log.info("ReAct iteration %d: executing %d tool call(s).", iteration + 1, len(response.tool_calls))
        for call in response.tool_calls:
            fn = tools.get(call["name"])
            result = fn.invoke({**call["args"], "repo_path": repo_path}) if fn else f"Unknown tool: {call['name']}"
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))

            if call["name"] in _FILE_WRITE_TOOLS:
                path = call["args"].get("path", "")
                if path and path not in modified_files:
                    modified_files.append(path)

    log.warning("ReAct loop reached max iterations (%d).", _MAX_LOOP_ITERATIONS)
    for msg in reversed(messages):
        if hasattr(msg, "content") and isinstance(msg.content, str) and msg.content:
            _, _, rest = msg.content.partition("### TESTS")
            return modified_files, rest.strip()
    return modified_files, ""


def _parse_tester_verdict(raw: str) -> str:
    """Extracts APPROVED or NEEDS_WORK from the first line of tester output."""
    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    if "APPROVED" in first_line.upper():
        return "APPROVED"
    return "NEEDS_WORK"


def _parse_plan_steps(text: str) -> list[str]:
    """Extracts the numbered steps from the planner's 'Ordered steps' section."""
    import re
    steps = []
    in_ordered_steps = False
    for line in text.splitlines():
        if "ordered steps" in line.lower():
            in_ordered_steps = True
            continue
        if in_ordered_steps:
            m = re.match(r"^\s*(\d+)[.)]\s+(.+)", line)
            if m:
                steps.append(m.group(2).strip())
            elif steps and line.strip().startswith("#"):
                break  # next markdown section — stop
    return steps or [text.strip()]  # fallback: treat whole plan as one step


def plan_solution(state: AgentState) -> dict:
    """Generates a structured implementation plan and parses it into ordered steps.

    Includes tester feedback so the planner can adjust on retry cycles.
    Result is cached per retry_count (L1 local + L2 Redis 60 s).
    Resets step_idx, step_results, and modified_files for the new execution cycle.
    """
    key_str = f"node:plan:{state['user_id']}:{state['repo_name']}:{state['issue_number']}:{state['retry_count']}"
    cached = _node_cache_get(key_str)
    if cached is not None:
        log.info("plan_solution: cache hit (attempt %d) — skipping LLM call.", state["retry_count"] + 1)
        return cached

    log.info("Planning solution for issue #%d (attempt %d)...", state["issue_number"], state["retry_count"] + 1)
    messages = _PLAN_PROMPT.format_messages(
        issue=state["issue_details"],
        context=state["project_context"],
        tester_feedback=state.get("tester_feedback") or "None — first attempt.",
    )
    response = llm.invoke(messages)
    steps = _parse_plan_steps(response.content)
    log.info("Plan parsed into %d step(s).", len(steps))

    result = {
        "plan": steps,
        "step_idx": 0,
        "step_results": [],
        "modified_files": [],
        "code_snippet": "",
    }
    _node_cache_set(key_str, result)
    return result


def execute_step(state: AgentState) -> dict:
    """Executes one step of the implementation plan via a ReAct loop.

    Accumulates modified_files and step_results across iterations.
    The last step's test_code is forwarded to the test node.
    Loops back via route_after_execute until all steps are done.
    """
    step_idx = state["step_idx"]
    plan = state["plan"]
    step = plan[step_idx]
    plan_str = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan))

    log.info(
        "Executing step %d/%d (attempt %d): %s",
        step_idx + 1, len(plan), state["retry_count"] + 1, step[:80],
    )

    repo_path = Path(state["local_repo_path"])
    new_files, test_code = _run_react_loop(
        _STEP_PROMPT,
        {
            "plan": plan_str,
            "step_num": step_idx + 1,
            "total_steps": len(plan),
            "step": step,
            "issue": state["issue_details"],
            "context": state["project_context"],
            "tester_feedback": state.get("tester_feedback") or "None — first attempt.",
        },
        str(repo_path),
    )

    # Accumulate modified files (deduplicated) across steps
    existing = state.get("modified_files", [])
    all_modified = existing + [f for f in new_files if f not in existing]

    file_contents = {
        f: (repo_path / f).read_text()
        for f in all_modified
        if (repo_path / f).exists()
    }
    code_snippet = "\n\n".join(
        f"# --- {f} ---\n{content}" for f, content in file_contents.items()
    )

    existing_results = state.get("step_results", [])
    step_summary = f"Step {step_idx + 1}: {step}"

    updates: dict = {
        "step_idx": step_idx + 1,
        "step_results": existing_results + [step_summary],
        "modified_files": all_modified,
        "code_snippet": code_snippet,
    }
    if test_code:
        updates["test_code"] = test_code
    return updates


def reflect(state: AgentState) -> dict:
    """Summarizes what was accomplished across all execute_step iterations.

    Informational only — the test node picks up modified_files and test_code
    directly from the state left by execute_step.
    """
    plan_str = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(state.get("plan", [])))
    summary = "\n".join(state.get("step_results", []))
    log.info(
        "Reflect — %d step(s) completed.\nPlan:\n%s\nResults:\n%s",
        len(state.get("plan", [])), plan_str, summary,
    )
    return {}


def test_code(state: AgentState) -> dict:
    """Runs the generated test file directly in the local repository.

    Dispatches the correct test command based on skills.test_runner.
    Returns 'success' status if tests pass, 'retry' if they fail.
    """
    log.info("Running tests in local repo...")
    test_runner = state.get("skills", {}).get("test_runner", "pytest")
    result = execute_tests(state["test_code"], test_runner, state["local_repo_path"])
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
    plan_str = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(state.get("plan", [])))
    messages = _TESTER_PROMPT.format_messages(
        plan=plan_str,
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
