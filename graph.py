from langgraph.graph import END, START, StateGraph

from actions.context_nodes import load_project_context
from actions.git_nodes import create_pr, handle_failure, preflight, read_github_issue
from actions.llm_nodes import execute_step, plan_solution, reflect, test_code, tester_review
from state import (
    AgentState,
    route_after_context,
    route_after_execute,
    route_after_preflight,
    route_after_test,
    route_after_tester,
)


def build_graph(checkpointer=None):
    """Builds and compiles the LangGraph state machine with a plan-execute-reflect loop.

    Workflow:
      preflight → load_project_context → read_issue → plan
        → execute_step (loops per plan step) → reflect → test → (conditional routing)

    preflight clones the repo if missing; any failure routes to fail_state immediately.

    Execute loop:
      plan → execute_step → route_after_execute:
        more steps → execute_step (loop)
        done       → reflect → test

    On test success:
      test → create_pr → END

    On test failure:
      test → tester → route_after_tester:
        APPROVED                → create_pr → END
        NEEDS_WORK, retries left → plan (re-plans with feedback, resets step_idx)
        NEEDS_WORK, max retries  → fail_state → END

    """
    workflow = StateGraph(AgentState)

    workflow.add_node("preflight", preflight)
    workflow.add_node("load_project_context", load_project_context)
    workflow.add_node("read_issue", read_github_issue)
    workflow.add_node("plan", plan_solution)
    workflow.add_node("execute_step", execute_step)
    workflow.add_node("reflect", reflect)
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
    workflow.add_edge("plan", "execute_step")
    workflow.add_conditional_edges(
        "execute_step",
        route_after_execute,
        {"execute_step": "execute_step", "reflect": "reflect"},
    )
    workflow.add_edge("reflect", "test")
    workflow.add_conditional_edges(
        "test",
        route_after_test,
        {"create_pr": "create_pr", "tester_review": "tester"},
    )
    workflow.add_conditional_edges(
        "tester",
        route_after_tester,
        {"create_pr": "create_pr", "plan": "plan", "handle_failure": "fail_state"},
    )
    workflow.add_edge("create_pr", END)
    workflow.add_edge("fail_state", END)

    return workflow.compile(checkpointer=checkpointer)
