from typing import TypedDict, Literal


class AgentState(TypedDict):
    user_id: str              # authenticated user's ID (used for cache key)
    repo_name: str            # "owner/repo"
    issue_number: int
    local_repo_path: str      # path to local clone of the repo
    base_branch: str          # e.g. "main"
    skills: dict              # parsed from skills.sh (language, framework, test_runner, ...)
    project_context: str      # compressed file tree + function index (cached)
    issue_details: str
    implementation_plan: str  # structured plan produced by plan_solution
    code_snippet: str
    test_code: str            # unit tests written by the coder
    test_results: str         # raw execution output
    tester_feedback: str      # structured failure analysis from tester_review
    retry_count: int
    max_retries: int
    status: str
    pr_url: str


def route_after_context(state: AgentState) -> Literal["read_issue", "fail_state"]:
    """Routes after load_project_context: skip to fail_state if repo validation failed."""
    if state.get("status") == "repo_mismatch":
        return "fail_state"
    return "read_issue"


def route_after_test(state: AgentState) -> Literal["create_pr", "tester_review"]:
    """On success go straight to PR; on failure route through tester for feedback."""
    if state["status"] == "success":
        return "create_pr"
    return "tester_review"


def route_after_tester(state: AgentState) -> Literal["create_pr", "code_solution", "handle_failure"]:
    """Tester has produced feedback.

    APPROVED verdict triggers early termination → create PR immediately.
    NEEDS_WORK retries the coder if iterations remain, otherwise fails.
    """
    if state.get("status") == "tester_approved":
        return "create_pr"
    if state["retry_count"] < state["max_retries"]:
        return "code_solution"
    return "handle_failure"
