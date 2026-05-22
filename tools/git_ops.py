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


def _authenticated_remote_url(remote_url: str) -> str:
    """Injects GitHub token into HTTPS remote URL for passwordless authentication."""
    token = os.getenv("GITHUB_TOKEN")
    return re.sub(r"https://([^@]*@)?", f"https://x-access-token:{token}@", remote_url)


def _get_auth_url(local_repo_path: str) -> str:
    if not os.getenv("GITHUB_TOKEN"):
        raise EnvironmentError("GITHUB_TOKEN is not set.")
    origin_url = _run(["git", "remote", "get-url", "origin"], cwd=local_repo_path)
    return _authenticated_remote_url(origin_url)


def _branch_exists_locally(local_repo_path: str, branch_name: str) -> bool:
    """Checks if a branch exists in the local repository."""
    result = _run(["git", "branch", "--list", branch_name], cwd=local_repo_path)
    return bool(result.strip())


def _checkout_or_create_branch(local_repo_path: str, branch_name: str) -> None:
    if _branch_exists_locally(local_repo_path, branch_name):
        _run(["git", "checkout", branch_name], cwd=local_repo_path)
    else:
        _run(["git", "checkout", "-b", branch_name], cwd=local_repo_path)


def ensure_repo_cloned(remote_repo_git: str, local_repo_path: str) -> None:
    """Clones remote_repo_git to local_repo_path if the directory is missing or not a git repo.

    No-op if the repo is already present.
    """
    repo_path = Path(local_repo_path)
    if not repo_path.exists() or not (repo_path / ".git").exists():
        log.info("Local path '%s' missing — cloning from remote...", local_repo_path)
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        remote_url = _authenticated_remote_url(f"https://github.com/{remote_repo_git}")
        _run(["git", "clone", remote_url, local_repo_path], cwd=str(repo_path.parent))
        log.info("Cloned '%s' to '%s'.", remote_repo_git, local_repo_path)


def create_branch_and_commit(
    local_repo_path: str,
    branch_name: str,
    base_branch: str,
    modified_files: list[str],
) -> None:
    """Stashes agent edits, syncs to base_branch, creates feature branch, pops stash, commits.

    This order preserves the ReAct agent's in-place file modifications while
    ensuring the branch starts from a clean, up-to-date base.

    Args:
        local_repo_path: Path to local repository clone.
        branch_name: Name of feature branch to create.
        base_branch: Base branch to branch from (e.g. 'main').
        modified_files: Repo-relative paths written by the ReAct agent.
    """
    auth_url = _get_auth_url(local_repo_path)

    log.info("Stashing agent edits before branch sync...")
    _run(["git", "stash", "--include-untracked"], cwd=local_repo_path)

    log.info("Fetching latest '%s' from origin...", base_branch)
    _run(["git", "fetch", auth_url, base_branch], cwd=local_repo_path)
    _run(["git", "checkout", base_branch], cwd=local_repo_path)
    _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=local_repo_path)

    _checkout_or_create_branch(local_repo_path, branch_name)

    log.info("Restoring agent edits onto '%s'...", branch_name)
    _run(["git", "stash", "pop"], cwd=local_repo_path)

    for f in modified_files:
        _run(["git", "add", f], cwd=local_repo_path)

    status = _run(["git", "status", "--porcelain"], cwd=local_repo_path)
    if not status:
        log.info("No changes to commit — branch is already up to date.")
    else:
        issue_num = branch_name.split("-")[-1]
        _run(
            ["git", "commit", "-m", f"feat: implement solution for issue #{issue_num}"],
            cwd=local_repo_path,
        )

    log.info("Pushing branch '%s'...", branch_name)
    _run(["git", "push", "-u", auth_url, branch_name], cwd=local_repo_path)
    log.info("Branch '%s' pushed to origin.", branch_name)
