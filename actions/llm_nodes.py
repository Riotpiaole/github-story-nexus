import logging

from langchain_core.prompts import ChatPromptTemplate

from config import llm, settings
from state import AgentState
from tools.executor import execute_python_tests

log = logging.getLogger(__name__)

_CODE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are an expert software engineer. Write clean Python code that solves the user's issue.\n"
        "You have access to code search tools to understand the project structure. "
        "Use them to find existing patterns, functions, and conventions before generating code.\n"
        "After searching (if needed), output ONLY valid, executable Python code. No markdown fences, no explanations."
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
