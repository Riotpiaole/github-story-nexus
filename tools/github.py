import logging
import requests
import time
from os import environ
from pathlib import Path

from github import Github, GithubIntegration, GithubException
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import get_settings


log = logging.getLogger(__name__)

_REQUIRED_PAT_SCOPES = {"repo"}



_transient = retry_if_exception_type(GithubException)
_backoff = wait_exponential(multiplier=1, min=2, max=30)



def _get_githubApp_auth_token(s):
    """Returns a GitHub App installation access token object."""
    private_key = Path(s.github_private_key_path).read_text()
    return GithubIntegration(int(s.github_app_id), private_key).get_access_token(s.github_installation_id)


def check_github_permissions() -> str | None:
    """Verifies the configured GitHub credentials have permission to push and create PRs.

    Returns an error string if permissions are insufficient, None if everything is OK.
    Used by the preflight node and test_tool.

    Note: OAuth App client+secret credentials (GITHUB_CLIENT + GITHUB_SECRET) are verified
    only for API connectivity — they cannot access private repos or create PRs unless the
    repo is public.
    """
    s = get_settings()

    # github oauth app authentication
    if s.github_client and s.github_secret:
        try:
            get_github_auth_token(s.github_client)
        except Exception as exc:
            return f"GitHub OAuth App credentials invalid: {exc}"
        return None

    # githubApp InstalltionId authentication
    if s.github_app_id and s.github_private_key_path:
        auth = _get_githubApp_auth_token(s)
        perms = getattr(auth, "permissions", {}) or {}
        pr_perm       = perms.get("pull_requests", "none") if isinstance(perms, dict) else getattr(perms, "pull_requests", "none")
        contents_perm = perms.get("contents", "none")      if isinstance(perms, dict) else getattr(perms, "contents", "none")
        issues = []
        if pr_perm not in ("write", "admin"):
            issues.append(f"pull_requests={pr_perm!r} (need 'write')")
        if contents_perm not in ("write", "admin"):
            issues.append(f"contents={contents_perm!r} (need 'write')")

        if issues:
            return f"GitHub App missing permissions: {', '.join(issues)}"
        
        environ["GITHUB_TOKEN"] = auth.token
        return None

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

    return "No GitHub credentials configured. Set GITHUB_TOKEN, GITHUB_APP_ID, or GITHUB_CLIENT+GITHUB_SECRET."


def _get_client() -> Github:
    """Authenticates with GitHub using the first available credential set.

    Priority: GitHub App > PAT > OAuth device flow.
    Raises EnvironmentError if no credentials are configured.
    """
    s = get_settings()
    if s.github_app_id and s.github_private_key_path:
        log.info("Authenticating via GitHub App (app_id=%s)", s.github_app_id)
        return Github(_get_githubApp_auth_token(s).token)

    if s.github_token:
        log.info("Authenticating via Personal Access Token.")
        return Github(s.github_token)

    if s.github_client:
        log.info("Authenticating via OAuth device flow.")
        return Github(get_github_auth_token(s.github_client))

    raise EnvironmentError(
        "No GitHub credentials found. Set GITHUB_TOKEN, GITHUB_APP_ID, or GITHUB_CLIENT."
    )


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




def get_github_auth_token(client_id: str) -> str:
    """Returns a GitHub access token via the device OAuth flow.

    Returns the cached GITHUB_TOKEN env var if set (and not the '#' re-auth sentinel).
    Otherwise initiates the GitHub device flow, polls until the user approves, then
    caches the resulting token in GITHUB_TOKEN for subsequent calls.
    """
    token = environ.get("GITHUB_TOKEN")
    if token and token != "#":
        return token

    device_code_resp = requests.post(
        "https://github.com/login/device/code",
        headers={"Accept": "application/json"},
        json={"client_id": client_id, "scope": " ".join(_REQUIRED_PAT_SCOPES)},
    )
    device_code_resp.raise_for_status()
    data = device_code_resp.json()

    print(f"\nOpen {data['verification_uri']} and enter code: {data['user_code']}\n")

    token = _poll_for_oauth_token(data["device_code"], data["interval"], client_id)
    environ["GITHUB_TOKEN"] = token
    return token


def _poll_for_oauth_token(device_code: str, interval: int, client_id: str) -> str:
    while True:
        time.sleep(interval)
        resp = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json={
                "client_id":   client_id,
                "device_code": device_code,
                "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        data = resp.json()

        match data.get("error"):
            case "authorization_pending":
                continue                      # user hasn't approved yet, keep waiting
            case "slow_down":
                interval += 5                 # GitHub asked us to back off
                continue
            case "expired_token":
                raise RuntimeError("Code expired — restart the flow")
            case "access_denied":
                raise RuntimeError("User rejected the request")
            case None:
                return data["access_token"]   # done
