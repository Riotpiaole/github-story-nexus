import os
from github import Github, GithubException


def _get_client() -> Github:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN environment variable is not set.")
    return Github(token)


def fetch_issue_from_github(repo_name: str, issue_number: int) -> dict:
    print(f"-> [GitHub API] Fetching issue #{issue_number} from {repo_name}...")
    g = _get_client()
    repo = g.get_repo(repo_name)
    issue = repo.get_issue(number=issue_number)
    return {
        "title": issue.title,
        "body": issue.body or "",
    }


def open_pull_request(repo_name: str, branch_name: str, base_branch: str, title: str, body: str) -> str:
    print(f"-> [GitHub API] Opening PR from '{branch_name}' into '{base_branch}' on {repo_name}...")
    g = _get_client()
    repo = g.get_repo(repo_name)
    pr = repo.create_pull(
        title=title,
        body=body,
        head=branch_name,
        base=base_branch,
    )
    return pr.html_url
