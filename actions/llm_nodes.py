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


def plan_solution(state: AgentState) -> dict:
    """Generates a structured implementation plan from the issue and cached project context.

    Uses a plain LLM call (no tools) — project_context is already a complete repo summary.
    Result is cached (L1 local + L2 Redis 60 s) so a downstream failure doesn't force a redo.
    """
    key_str = f"node:plan:{state['user_id']}:{state['repo_name']}:{state['issue_number']}"
    cached = _node_cache_get(key_str)
    if cached is not None:
        log.info("plan_solution: cache hit for issue #%d — skipping LLM call.", state["issue_number"])
        return cached

    log.info("Planning solution for issue #%d...", state["issue_number"])
    messages = _PLAN_PROMPT.format_messages(
        issue=state["issue_details"],
        context=state["project_context"],
    )
    response = llm.invoke(messages)
    result = {"implementation_plan": response.content}
    _node_cache_set(key_str, result)
    return result


def code_solution(state: AgentState) -> dict:
    """Modifies repo files in-place via a ReAct loop and returns unit test code.

    The agent reads existing files, applies targeted edits, then emits only
    ### TESTS in its final response. Modified file contents are concatenated
    into code_snippet for the sandboxed executor.

    Result is cached per retry attempt (L1 local + L2 Redis 60 s). On a cache
    hit, file writes are re-applied to the repo before returning so the working
    tree is consistent even if the process recovered from a downstream failure.
    """
    key_str = f"node:code:{state['user_id']}:{state['repo_name']}:{state['issue_number']}:{state['retry_count']}"
    cached = _node_cache_get(key_str)
    if cached is not None:
        log.info("code_solution: cache hit (attempt %d) — restoring file writes.", state["retry_count"] + 1)
        repo_path = Path(state["local_repo_path"])
        for rel_path, content in cached.get("file_contents", {}).items():
            target = repo_path / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return {k: cached[k] for k in ("modified_files", "code_snippet", "test_code")}

    log.info("Generating solution (attempt %d/%d)...", state["retry_count"] + 1, settings.max_retries)
    repo_path = Path(state["local_repo_path"])
    modified_files, test_code = _run_react_loop(
        _CODE_PROMPT,
        {
            "plan": state.get("implementation_plan", ""),
            "issue": state["issue_details"],
            "context": state["project_context"],
            "tester_feedback": state.get("tester_feedback") or "None — first attempt.",
        },
        str(repo_path),
    )
    file_contents = {
        f: (repo_path / f).read_text()
        for f in modified_files
        if (repo_path / f).exists()
    }
    code_snippet = "\n\n".join(
        f"# --- {f} ---\n{content}" for f, content in file_contents.items()
    )
    result = {"modified_files": modified_files, "code_snippet": code_snippet, "test_code": test_code}
    _node_cache_set(key_str, {**result, "file_contents": file_contents})
    return result


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
