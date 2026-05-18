from typing import TypedDict, Literal


class AgentState(TypedDict):
    repo_name: str        # "owner/repo"
    issue_number: int
    local_repo_path: str  # path to local clone of the repo
    base_branch: str      # e.g. "main"
    issue_details: str
    code_snippet: str
    test_code: str        # LLM-generated pytest file
    test_results: str
    retry_count: int
    max_retries: int
    status: str
    pr_url: str


def route_after_test(state: AgentState) -> Literal["create_pr", "code_solution", "handle_failure"]:
    if state["status"] == "success":
        return "create_pr"
    if state["retry_count"] < state["max_retries"]:
        return "code_solution"
    return "handle_failure"
