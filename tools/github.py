import logging
from pathlib import Path

from github import Github, GithubIntegration, GithubException
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import get_settings

log = logging.getLogger(__name__)

_REQUIRED_PAT_SCOPES = {"repo", "public_repo"}


def _get_app_auth(s):
    """Returns a GitHub App installation access token object."""
    private_key = Path(s.github_private_key_path).read_text()
    return GithubIntegration(int(s.github_app_id), private_key).get_access_token(s.github_installation_id)


def check_github_permissions() -> str | None:
    """Verifies the configured GitHub credentials have permission to push and create PRs.

    Returns an error string if permissions are insufficient, None if everything is OK.
    Used by the preflight node and test_tool.
    """
    s = get_settings()

    if s.github_app_id and s.github_private_key_path:
        auth = _get_app_auth(s)
        perms = getattr(auth, "permissions", {}) or {}
        pr_perm       = perms.get("pull_requests", "none") if isinstance(perms, dict) else getattr(perms, "pull_requests", "none")
        contents_perm = perms.get("contents", "none")      if isinstance(perms, dict) else getattr(perms, "contents", "none")
        issues = []
        if pr_perm not in ("write", "admin"):
            issues.append(f"pull_requests={pr_perm!r} (need 'write')")
        if contents_perm not in ("write", "admin"):
            issues.append(f"contents={contents_perm!r} (need 'write')")
        return f"GitHub App missing permissions: {', '.join(issues)}" if issues else None

    if s.github_token:
        g = Github(s.github_token)
        g.get_user()
        scopes = set(g.oauth_scopes or [])
        if not (_REQUIRED_PAT_SCOPES & scopes):
            return (
                f"GitHub PAT missing 'repo' or 'public_repo' scope — cannot push or create PRs. "
                f"Current scopes: {sorted(scopes) or 'none'}"
            )
        return None

    return "No GitHub credentials configured. Set GITHUB_TOKEN or GITHUB_APP_ID."


def _get_client() -> Github:
    """Authenticates with GitHub using either GitHub App or Personal Access Token.

    Raises EnvironmentError if no credentials are configured.
    """
    s = get_settings()
    if s.github_app_id and s.github_private_key_path:
        log.info("Authenticating via GitHub App (app_id=%s)", s.github_app_id)
        return Github(_get_app_auth(s).token)

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


def create_draft_pr(repo_name: str, branch_name: str, base_branch: str) -> int:
    """Creates a draft placeholder PR for the preflight check.

    Returns the PR number so create_pr can later promote it to a real PR.
    """
    g = _get_client()
    repo = g.get_repo(repo_name)
    pr = repo.create_pull(
        title="[preflight] permission check",
        body="Automated preflight — will be replaced with the real implementation.",
        head=branch_name,
        base=base_branch,
        draft=True,
    )
    log.info("Draft preflight PR #%d created: %s", pr.number, pr.html_url)
    return pr.number


def update_pull_request(repo_name: str, pr_number: int, title: str, body: str) -> str:
    """Promotes the draft preflight PR to a ready PR with the real title and body.

    Returns the PR HTML URL.
    """
    g = _get_client()
    pr = g.get_repo(repo_name).get_pull(pr_number)
    pr.edit(title=title, body=body, draft=False)
    log.info("PR #%d promoted to ready: %s", pr_number, pr.html_url)
    return pr.html_url
