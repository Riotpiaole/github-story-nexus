from typing import TypedDict, Literal


class AgentState(TypedDict):
    user_id: str              # authenticated user's ID (used for cache key)
    repo_name: str            # "owner/repo"
    issue_number: int
    local_repo_path: str      # path to local clone of the repo
    base_branch: str          # e.g. "main"
    preflight_error: str      # human-readable failure reason set when preflight fails
    skills: dict              # parsed from skills.sh (language, framework, test_runner, ...)
    project_context: str      # compressed file tree + function index (cached)
    issue_details: str
    plan: list[str]           # ordered implementation steps parsed from plan_solution
    step_idx: int             # index of the step currently being executed
    step_results: list[str]   # per-step summaries accumulated by execute_step
    modified_files: list[str]  # repo-relative paths written across all execute_step iterations
    code_snippet: str          # concatenated content of modified_files (for the tester)
    test_code: str             # unit tests emitted by the final execute_step
    test_results: str         # raw execution output
    tester_feedback: str      # structured failure analysis from tester_review
    retry_count: int
    max_retries: int
    status: str
    pr_url: str
    github_token: str


def route_after_execute(state: AgentState) -> Literal["execute_step", "reflect"]:
    """Loops execute_step until all plan steps are done, then routes to reflect."""
    if state["step_idx"] < len(state["plan"]):
        return "execute_step"
    return "reflect"


def route_after_preflight(state: AgentState) -> Literal["load_project_context", "fail_state"]:
    """Routes after preflight: fail immediately if any permission check failed."""
    if state.get("status") == "preflight_failed":
        return "fail_state"
    return "load_project_context"


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


def route_after_tester(state: AgentState) -> Literal["create_pr", "plan", "handle_failure"]:
    """Tester has produced feedback.

    APPROVED verdict triggers early termination → create PR immediately.
    NEEDS_WORK re-plans with tester feedback if retries remain, otherwise fails.
    """
    if state.get("status") == "tester_approved":
        return "create_pr"
    if state["retry_count"] < state["max_retries"]:
        return "plan"
    return "handle_failure"
