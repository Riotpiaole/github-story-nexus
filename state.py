from typing import TypedDict, Literal


class AgentState(TypedDict):
    repo_name: str        # "owner/repo"
    issue_number: int
    local_repo_path: str  # path to local clone of the repo
    base_branch: str      # e.g. "main"
    skills: dict          # parsed from skills.sh (language, framework, test_runner, ...)
    project_context: str  # compressed file tree + function index
    issue_details: str
    code_snippet: str
    test_code: str        # LLM-generated pytest file
    test_results: str
    retry_count: int
    max_retries: int
    status: str
    pr_url: str


def route_after_context(state: AgentState) -> Literal["read_issue", "fail_state"]:
    """Routes after load_project_context: skip to fail_state if repo validation failed."""
    if state.get("status") == "repo_mismatch":
        return "fail_state"
    return "read_issue"


def route_after_test(state: AgentState) -> Literal["create_pr", "code_solution", "handle_failure"]:
    """Routes the workflow after test execution.

    Returns 'create_pr' if tests pass, 'code_solution' to retry if retries remain,
    or 'handle_failure' if max retries exceeded.
    """
    if state["status"] == "success":
        return "create_pr"
    if state["retry_count"] < state["max_retries"]:
        return "code_solution"
    return "handle_failure"
