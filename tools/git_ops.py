import os
import re
import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: str):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git command {cmd} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _authenticated_remote_url(remote_url: str, token: str) -> str:
    """Injects the GitHub token into an HTTPS remote URL."""
    # Handles: https://github.com/... and https://user@github.com/...
    return re.sub(r"https://([^@]*@)?", f"https://x-access-token:{token}@", remote_url)


def create_branch_and_commit(local_repo_path: str, branch_name: str, base_branch: str, code: str, filename: str) -> None:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN environment variable is not set.")

    print(f"-> [Git] Creating branch '{branch_name}' from '{base_branch}'...")

    # Resolve the current remote URL and build an authenticated version for push/fetch
    origin_url = _run(["git", "remote", "get-url", "origin"], cwd=local_repo_path)
    auth_url = _authenticated_remote_url(origin_url, token)

    _run(["git", "fetch", auth_url, base_branch], cwd=local_repo_path)
    _run(["git", "checkout", base_branch], cwd=local_repo_path)
    _run(["git", "reset", "--hard", f"FETCH_HEAD"], cwd=local_repo_path)
    _run(["git", "checkout", "-b", branch_name], cwd=local_repo_path)

    file_path = Path(local_repo_path) / filename
    file_path.write_text(code)
    print(f"-> [Git] Written code to {file_path}")

    _run(["git", "add", filename], cwd=local_repo_path)
    _run(["git", "commit", "-m", f"feat: implement solution for {branch_name}"], cwd=local_repo_path)

    # Push using the authenticated URL so the token is never stored in git config
    _run(["git", "push", "-u", auth_url, branch_name], cwd=local_repo_path)
    print(f"-> [Git] Pushed branch '{branch_name}' to origin.")
