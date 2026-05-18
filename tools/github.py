import logging
from pathlib import Path

from github import Github, GithubIntegration, GithubException
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import get_settings

log = logging.getLogger(__name__)


def _get_client() -> Github:
    """Authenticates with GitHub using either GitHub App or Personal Access Token.

    Raises EnvironmentError if no credentials are configured.
    """
    s = get_settings()
    if s.github_app_id and s.github_private_key_path:
        log.info("Authenticating via GitHub App (app_id=%s)", s.github_app_id)
        private_key = Path(s.github_private_key_path).read_text()
        integration = GithubIntegration(int(s.github_app_id), private_key)
        token = integration.get_access_token(s.github_installation_id).token
        return Github(token)

    if s.github_token:
        log.info("Authenticating via Personal Access Token.")
        return Github(s.github_token)

    raise EnvironmentError(
        "No GitHub credentials found. Set GITHUB_TOKEN (PAT) or "
        "GITHUB_APP_ID + GITHUB_PRIVATE_KEY_PATH + GITHUB_INSTALLATION_ID."
    )


_transient = retry_if_exception_type(GithubException)
_backoff = wait_exponential(multiplier=1, min=2, max=30)


@retry(retry=_transient, stop=stop_after_attempt(3), wait=_backoff, reraise=True)
def fetch_issue_from_github(repo_name: str, issue_number: int) -> dict:
    """Fetches issue title and body from GitHub with exponential backoff retry.

    Args:
        repo_name: Repository in 'owner/repo' format.
        issue_number: GitHub issue number.

    Returns:
        Dict with 'title' and 'body' keys.
    """
    log.info("Fetching issue #%d from %s...", issue_number, repo_name)
    g = _get_client()
    repo = g.get_repo(repo_name)
    issue = repo.get_issue(number=issue_number)
    return {"title": issue.title, "body": issue.body or ""}


@retry(retry=_transient, stop=stop_after_attempt(3), wait=_backoff, reraise=True)
def open_pull_request(repo_name: str, branch_name: str, base_branch: str, title: str, body: str) -> str:
    """Opens a pull request, or returns existing URL if PR already exists for this branch.

    Idempotent: safe to call multiple times.

    Args:
        repo_name: Repository in 'owner/repo' format.
        branch_name: Feature branch name.
        base_branch: Base branch (e.g. 'main').
        title: PR title.
        body: PR description.

    Returns:
        The HTML URL of the pull request.
    """
    log.info("Opening PR from '%s' into '%s' on %s...", branch_name, base_branch, repo_name)
    g = _get_client()
    repo = g.get_repo(repo_name)

    # Idempotent: return existing PR if one is already open for this branch
    open_prs = repo.get_pulls(state="open", head=f"{repo_name.split('/')[0]}:{branch_name}")
    if open_prs.totalCount > 0:
        existing = open_prs[0]
        log.info("PR already exists: %s", existing.html_url)
        return existing.html_url

    pr = repo.create_pull(title=title, body=body, head=branch_name, base=base_branch)
    log.info("PR created: %s", pr.html_url)
    return pr.html_url
