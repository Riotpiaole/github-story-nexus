from state import AgentState
from config import llm
from tools.executor import execute_python_tests
from langchain_core.prompts import ChatPromptTemplate

_CODE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "You are an expert software engineer. Write clean Python code that solves the user's issue.\n"
        "CRITICAL: Output ONLY valid, executable Python code. No markdown fences, no explanations."
    )),
    ("user", "GitHub Issue:\n{issue}\n\nPrevious test failure (empty if first attempt):\n{test_feedback}"),
])


def code_solution(state: AgentState) -> dict:
    print("--- CODING SOLUTION WITH CLAUDE ---")
    chain = _CODE_PROMPT | llm
    feedback = state.get("test_results") or "None — this is the first attempt."
    response = chain.invoke({
        "issue": state["issue_details"],
        "test_feedback": feedback,
    })
    return {"code_snippet": response.content.strip()}


def test_code(state: AgentState) -> dict:
    print("--- TESTING CODE ---")
    test_run = execute_python_tests(state["code_snippet"])
    if test_run["success"]:
        print("Tests passed:", test_run["output"])
        return {"test_results": "passed", "status": "success"}
    else:
        print("Tests failed:", test_run["output"])
        return {
            "test_results": f"Execution failed:\n{test_run['output']}",
            "retry_count": state["retry_count"] + 1,
            "status": "retry",
        }
