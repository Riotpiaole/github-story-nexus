import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: str) -> str:
    """Runs a shell command and returns stdout, raises RuntimeError on failure."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {cmd[1]} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _authenticated_remote_url(remote_url: str, token: str) -> str:
    """Injects GitHub token into HTTPS remote URL for passwordless authentication."""
    return re.sub(r"https://([^@]*@)?", f"https://x-access-token:{token}@", remote_url)


def _branch_exists_locally(local_repo_path: str, branch_name: str) -> bool:
    """Checks if a branch exists in the local repository."""
    result = _run(["git", "branch", "--list", branch_name], cwd=local_repo_path)
    return bool(result.strip())


def create_branch_and_commit(
    local_repo_path: str,
    branch_name: str,
    base_branch: str,
    code: str,
    filename: str,
) -> None:
    """Creates a branch, writes code to file, commits, and pushes to origin.

    Idempotent: reuses existing branch/commit if no changes are detected.
    Uses GITHUB_TOKEN from environment for authenticated push.

    Args:
        local_repo_path: Path to local repository clone.
        branch_name: Name of feature branch to create.
        base_branch: Base branch to branch from (e.g. 'main').
        code: Solution code to write to file.
        filename: Name of file to create in the repository.
    """
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN is not set.")

    origin_url = _run(["git", "remote", "get-url", "origin"], cwd=local_repo_path)
    auth_url = _authenticated_remote_url(origin_url, token)

    log.info("Fetching latest '%s' from origin...", base_branch)
    _run(["git", "fetch", auth_url, base_branch], cwd=local_repo_path)
    _run(["git", "checkout", base_branch], cwd=local_repo_path)
    _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=local_repo_path)

    # Idempotent: reuse branch if it already exists locally
    if _branch_exists_locally(local_repo_path, branch_name):
        log.info("Branch '%s' already exists locally — checking it out.", branch_name)
        _run(["git", "checkout", branch_name], cwd=local_repo_path)
    else:
        log.info("Creating branch '%s'...", branch_name)
        _run(["git", "checkout", "-b", branch_name], cwd=local_repo_path)

    file_path = Path(local_repo_path) / filename
    file_path.write_text(code)
    log.info("Written code to %s", file_path)

    _run(["git", "add", filename], cwd=local_repo_path)

    # Skip commit if nothing changed (idempotent re-run)
    status = _run(["git", "status", "--porcelain"], cwd=local_repo_path)
    if not status:
        log.info("No changes to commit — branch is already up to date.")
    else:
        _run(
            ["git", "commit", "-m", f"feat: implement solution for issue #{branch_name.split('-')[-1]}"],
            cwd=local_repo_path,
        )

    log.info("Pushing branch '%s'...", branch_name)
    _run(["git", "push", "-u", auth_url, branch_name], cwd=local_repo_path)
    log.info("Branch '%s' pushed to origin.", branch_name)
