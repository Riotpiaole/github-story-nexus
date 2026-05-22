import logging
import requests
import time
from os import environ
from pathlib import Path

from github import Github, GithubIntegration, GithubException
from tenacity import retry

from config import get_settings
from tools._retry import GITHUB_RETRY, RetryableError

log = logging.getLogger(__name__)

_REQUIRED_PAT_SCOPES = {"repo"}

_RETRY_REASONS: dict[int, str] = {
    429: "Rate limited: GitHub throttles excessive requests; back off and retry.",
    500: "Internal server error: GitHub backend fault; likely transient.",
    502: "Bad gateway: GitHub proxy / CDN fault; retry resolves most cases.",
    503: "Service unavailable: GitHub maintenance or overload; retry after backoff.",
    504: "Gateway timeout: Upstream GitHub timeout; retry with backoff.",
}


def _get_githubApp_auth_token(s):
    """Returns a GitHub App installation access token object."""
    private_key = Path(s.github_private_key_path).read_text()
    return GithubIntegration(int(s.github_app_id), private_key).get_access_token(s.github_installation_id)


class GitHubOAuthClient:
    """Wraps GitHub's device flow OAuth (CLI-friendly, no browser redirect).

    Retried HTTP errors on device-code and token-exchange POSTs:
      429 — Rate limited: GitHub throttles auth endpoints; back off.
      500 — Internal server error: GitHub auth service fault; likely transient.
      502 — Bad gateway: GitHub proxy fault; transient.
      503 — Service unavailable: GitHub auth maintenance; retry after backoff.

    Not retried:
      400 — Bad request (malformed client_id or grant type; permanent).
      401 — Unauthorized (invalid client_secret; permanent).

    Device-flow polling errors (JSON body, not HTTP status):
      authorization_pending — user hasn't approved yet; keep polling (not an error).
      slow_down             — GitHub asked us to increase interval; handled inline.
      expired_token         — code has expired; raises RuntimeError (non-retryable).
      access_denied         — user rejected; raises RuntimeError (non-retryable).
    """

    _RETRYABLE_STATUSES = {429, 500, 502, 503}

    def get_auth_token(self, client_id: str) -> str:
        """Returns a GitHub access token via the device OAuth flow.

        Returns the cached GITHUB_TOKEN env var if set (and not the '#' re-auth sentinel).
        Otherwise initiates the GitHub device flow, polls until the user approves, then
        caches the resulting token in GITHUB_TOKEN for subsequent calls.
        """
        token = environ.get("GITHUB_TOKEN")
        if token and token != "#":
            return token

        resp = requests.post(
            "https://github.com/login/device/code",
            headers={"Accept": "application/json"},
            json={"client_id": client_id, "scope": " ".join(_REQUIRED_PAT_SCOPES)},
        )
        if resp.status_code in self._RETRYABLE_STATUSES:
            raise RetryableError(
                resp.status_code,
                resp.text,
                _RETRY_REASONS.get(resp.status_code, "transient HTTP error"),
            )
        resp.raise_for_status()
        data = resp.json()

        print(f"\nOpen {data['verification_uri']} and enter code: {data['user_code']}\n")

        token = self._poll_for_token(data["device_code"], data["interval"], client_id)
        environ["GITHUB_TOKEN"] = token
        return token

    def _poll_for_token(self, device_code: str, interval: int, client_id: str) -> str:
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
                    continue
                case "slow_down":
                    interval += 5
                    continue
                case "expired_token":
                    raise RuntimeError("Code expired — restart the flow")
                case "access_denied":
                    raise RuntimeError("User rejected the request")
                case None:
                    return data["access_token"]


class GitHubClient:
    """Wraps PyGithub calls with documented retry on transient HTTP errors.

    Retried status codes:
      429 — Rate limited: GitHub throttles excessive requests; back off and retry.
      500 — Internal server error: GitHub backend fault; likely transient.
      502 — Bad gateway: GitHub proxy / CDN fault; retry resolves most cases.
      503 — Service unavailable: GitHub maintenance or overload; retry after backoff.
      504 — Gateway timeout: Upstream GitHub timeout; retry with backoff.

    Not retried:
      401 — Auth failure (permanent — wrong credentials).
      403 — Forbidden (wrong scope or private repo; not transient).
      404 — Not found (repo or issue does not exist; not transient).
      422 — Unprocessable (validation error; not transient).
    """

    _RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self) -> None:
        self._settings = get_settings()
        self._oauth = GitHubOAuthClient()

    def _get_client(self) -> Github:
        """Returns an authenticated PyGithub client (App > PAT > device flow)."""
        s = self._settings
        if s.github_app_id and s.github_private_key_path:
            log.info("Authenticating via GitHub App (app_id=%s)", s.github_app_id)
            return Github(_get_githubApp_auth_token(s).token)
        if s.github_token:
            log.info("Authenticating via Personal Access Token.")
            return Github(s.github_token)
        if s.github_client:
            log.info("Authenticating via OAuth device flow.")
            return Github(self._oauth.get_auth_token(s.github_client))
        raise EnvironmentError(
            "No GitHub credentials found. Set GITHUB_TOKEN, GITHUB_APP_ID, or GITHUB_CLIENT."
        )

    def _guard(self, exc: GithubException) -> None:
        """Converts a GithubException to RetryableError if its status is retryable."""
        if exc.status in self._RETRYABLE_STATUSES:
            raise RetryableError(
                exc.status,
                str(exc.data),
                _RETRY_REASONS.get(exc.status, "transient HTTP error"),
            )
        raise exc

    @retry(**GITHUB_RETRY)
    def fetch_issue(self, repo_name: str, issue_number: int) -> dict:
        """Fetches issue title and body from GitHub with exponential backoff retry."""
        log.info("Fetching issue #%d from %s...", issue_number, repo_name)
        try:
            g = self._get_client()
            repo = g.get_repo(repo_name)
            issue = repo.get_issue(number=issue_number)
            return {"title": issue.title, "body": issue.body or ""}
        except GithubException as exc:
            self._guard(exc)

    @retry(**GITHUB_RETRY)
    def open_pull_request(
        self,
        repo_name: str,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        """Opens a pull request, or returns existing URL if PR already exists for this branch.

        Idempotent: safe to call multiple times.
        """
        log.info("Opening PR from '%s' into '%s' on %s...", branch_name, base_branch, repo_name)
        try:
            g = self._get_client()
            repo = g.get_repo(repo_name)
            open_prs = repo.get_pulls(
                state="open", head=f"{repo_name.split('/')[0]}:{branch_name}"
            )
            if open_prs.totalCount > 0:
                existing = open_prs[0]
                log.info("PR already exists: %s", existing.html_url)
                return existing.html_url
            pr = repo.create_pull(title=title, body=body, head=branch_name, base=base_branch)
            log.info("PR created: %s", pr.html_url)
            return pr.html_url
        except GithubException as exc:
            self._guard(exc)

    def check_permissions(self) -> str | None:
        """Verifies the configured GitHub credentials have permission to push and create PRs.

        Returns an error string if permissions are insufficient, None if everything is OK.
        """
        s = self._settings

        if s.github_client and s.github_secret:
            try:
                self._oauth.get_auth_token(s.github_client)
            except Exception as exc:
                return f"GitHub OAuth App credentials invalid: {exc}"
            return None

        if s.github_app_id and s.github_private_key_path:
            auth = _get_githubApp_auth_token(s)
            perms = getattr(auth, "permissions", {}) or {}
            pr_perm = (
                perms.get("pull_requests", "none")
                if isinstance(perms, dict)
                else getattr(perms, "pull_requests", "none")
            )
            contents_perm = (
                perms.get("contents", "none")
                if isinstance(perms, dict)
                else getattr(perms, "contents", "none")
            )
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


# Module-level singleton — shared across all call sites
_github_client = GitHubClient()


def check_github_permissions() -> str | None:
    return _github_client.check_permissions()


def fetch_issue_from_github(repo_name: str, issue_number: int) -> dict:
    return _github_client.fetch_issue(repo_name, issue_number)


def open_pull_request(
    repo_name: str, branch_name: str, base_branch: str, title: str, body: str
) -> str:
    return _github_client.open_pull_request(repo_name, branch_name, base_branch, title, body)


def get_github_auth_token(client_id: str) -> str:
    return GitHubOAuthClient().get_auth_token(client_id)
