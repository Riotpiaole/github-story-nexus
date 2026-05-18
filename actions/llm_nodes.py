import logging

from langchain_core.prompts import ChatPromptTemplate

from config import llm, settings
from state import AgentState
from tools.executor import execute_python_tests

log = logging.getLogger(__name__)

_CODE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are an expert software engineer. Write clean Python code that solves the user's issue.\n"
        "CRITICAL: Output ONLY valid, executable Python code. No markdown fences, no explanations."
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
        "Cover happy paths and edge cases.\n"
        "CRITICAL: Output ONLY valid pytest code. No markdown fences, no explanations."
    )),
    ("user", (
        "GitHub Issue (what the code should do):\n{issue}\n\n"
        "Solution code:\n{code}"
    )),
])


def code_solution(state: AgentState) -> dict:
    log.info("Generating solution with Claude (attempt %d/%d)...", state["retry_count"] + 1, settings.max_retries)
    chain = _CODE_PROMPT | llm
    feedback = state.get("test_results") or "None — this is the first attempt."
    response = chain.invoke({
        "issue": state["issue_details"],
        "test_feedback": feedback,
    })
    return {"code_snippet": response.content.strip()}


def generate_tests(state: AgentState) -> dict:
    log.info("Generating pytest tests with Claude...")
    chain = _TEST_PROMPT | llm
    response = chain.invoke({
        "issue": state["issue_details"],
        "code": state["code_snippet"],
    })
    return {"test_code": response.content.strip()}


def test_code(state: AgentState) -> dict:
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
