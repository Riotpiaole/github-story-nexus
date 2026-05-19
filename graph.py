from langgraph.graph import StateGraph, START, END

from state import AgentState, route_after_context, route_after_test
from actions.context_nodes import load_project_context
from actions.git_nodes import read_github_issue, create_pr, handle_failure
from actions.llm_nodes import code_solution, generate_tests, test_code


def build_graph():
    """Builds and compiles the LangGraph state machine.

    Workflow: load_project_context → read_issue → code → generate_tests → test → (conditional routing)
    - On test success: create_pr → END
    - On test failure with retries left: loop back to code
    - On max retries exceeded: handle_failure → END

    Returns compiled workflow executable.
    """

    workflow = StateGraph(AgentState)
    workflow.add_node("load_project_context", load_project_context)
    workflow.add_node("read_issue", read_github_issue)
    workflow.add_node("code", code_solution)
    workflow.add_node("generate_tests", generate_tests)
    workflow.add_node("test", test_code)
    workflow.add_node("create_pr", create_pr)
    workflow.add_node("fail_state", handle_failure)

    workflow.add_edge(START, "load_project_context")
    workflow.add_conditional_edges(
        "load_project_context",
        route_after_context,
        {"read_issue": "read_issue", "fail_state": "fail_state"},
    )
    workflow.add_edge("read_issue", "code")
    workflow.add_edge("code", "generate_tests")
    workflow.add_edge("generate_tests", "test")
    workflow.add_conditional_edges(
        "test",
        route_after_test,
        {
            "create_pr": "create_pr",
            "code_solution": "code",
            "handle_failure": "fail_state",
        },
    )
    workflow.add_edge("create_pr", END)
    workflow.add_edge("fail_state", END)

    return workflow.compile()
