from langgraph.graph import StateGraph, START, END

from state import AgentState, route_after_preflight, route_after_context, route_after_test, route_after_tester
from actions.context_nodes import load_project_context
from actions.git_nodes import preflight, read_github_issue, create_pr, handle_failure
from actions.llm_nodes import plan_solution, code_solution, test_code, tester_review


def build_graph():
    """Builds and compiles the LangGraph state machine.

    Workflow:
      preflight → load_project_context → read_issue → plan → code → test → (conditional routing)

    preflight checks GitHub permissions (token scopes, push, draft PR) and ast-grep.
    Any preflight failure routes immediately to fail_state before any LLM work begins.

    On test success:
      test → create_pr (promotes draft PR) → END

    On test failure:
      test → tester (analyzes failure, produces feedback or APPROVED verdict)
        - APPROVED (early termination): tester → create_pr → END
        - NEEDS_WORK with retries left:  tester → code → test (loop)
        - NEEDS_WORK at max iterations:  tester → fail_state → END

    Returns compiled workflow executable.
    """
    workflow = StateGraph(AgentState)

    workflow.add_node("preflight", preflight)
    workflow.add_node("load_project_context", load_project_context)
    workflow.add_node("read_issue", read_github_issue)
    workflow.add_node("plan", plan_solution)
    workflow.add_node("code", code_solution)
    workflow.add_node("test", test_code)
    workflow.add_node("tester", tester_review)
    workflow.add_node("create_pr", create_pr)
    workflow.add_node("fail_state", handle_failure)

    workflow.add_edge(START, "preflight")
    workflow.add_conditional_edges(
        "preflight",
        route_after_preflight,
        {"load_project_context": "load_project_context", "fail_state": "fail_state"},
    )
    workflow.add_conditional_edges(
        "load_project_context",
        route_after_context,
        {"read_issue": "read_issue", "fail_state": "fail_state"},
    )
    workflow.add_edge("read_issue", "plan")
    workflow.add_edge("plan", "code")
    workflow.add_edge("code", "test")
    workflow.add_conditional_edges(
        "test",
        route_after_test,
        {"create_pr": "create_pr", "tester_review": "tester"},
    )
    workflow.add_conditional_edges(
        "tester",
        route_after_tester,
        {"create_pr": "create_pr", "code_solution": "code", "handle_failure": "fail_state"},
    )
    workflow.add_edge("create_pr", END)
    workflow.add_edge("fail_state", END)

    return workflow.compile()
