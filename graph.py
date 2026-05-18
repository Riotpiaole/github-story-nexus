from langgraph.graph import StateGraph, START, END
from state import AgentState, route_after_test
from actions.git_nodes import read_github_issue, create_pr, handle_failure
from actions.llm_nodes import code_solution, test_code


def build_graph():
    workflow = StateGraph(AgentState)

    workflow.add_node("read_issue", read_github_issue)
    workflow.add_node("code", code_solution)
    workflow.add_node("test", test_code)
    workflow.add_node("create_pr", create_pr)
    workflow.add_node("fail_state", handle_failure)

    workflow.add_edge(START, "read_issue")
    workflow.add_edge("read_issue", "code")
    workflow.add_edge("code", "test")
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
