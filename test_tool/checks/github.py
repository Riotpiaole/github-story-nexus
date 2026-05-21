"""
Diagnostic script for GitHub App authentication (PyGithub 2.x).

Run from the project root:
    python test_tool/checks/github.py [owner/repo]

Walks through each auth stage and prints PASS / FAIL at every step so you can
see exactly where your credentials break down.
"""

import sys
from pathlib import Path

# Make project root importable regardless of where the script is invoked from.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

from config import get_settings


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m  {msg}")

def _fail(msg: str, exc: Exception | None = None) -> None:
    print(f"  \033[31m✗\033[0m  {msg}")
    if exc:
        print(f"       {type(exc).__name__}: {exc}")

def _info(msg: str) -> None:
    print(f"     {msg}")

def _section(title: str) -> None:
    print(f"\n── {title}")


def check_settings() -> bool:
    _section("1. Settings / .env")
    s = get_settings()
    ok = True

    for field, val in [
        ("GITHUB_APP_ID",           s.github_app_id),
        ("GITHUB_PRIVATE_KEY_PATH", s.github_private_key_path),
        ("GITHUB_INSTALLATION_ID",  s.github_installation_id),
    ]:
        if val and val != 0:
            _ok(f"{field} = {val!r}")
        else:
            _fail(f"{field} is not set (current: {val!r})")
            ok = False

    return ok


def check_private_key(key_path: str) -> bytes | None:
    _section("2. Private key file")
    path = Path(key_path)

    if not path.exists():
        _fail(f"File not found: {path.resolve()}")
        return None
    _ok(f"File exists: {path.resolve()}")

    try:
        raw = path.read_bytes()
    except OSError as e:
        _fail("Cannot read file", e)
        return None

    if b"BEGIN RSA PRIVATE KEY" in raw or b"BEGIN PRIVATE KEY" in raw:
        _ok("PEM header detected")
    else:
        _fail("PEM header not found — file may not be a valid private key")
        return None

    _info(f"Size: {len(raw)} bytes")
    return raw


def check_app_auth(app_id: str, private_key: str) -> object | None:
    _section("3. AppAuth — JWT generation (PyGithub 2.x)")
    try:
        from github import Auth
        app_auth = Auth.AppAuth(int(app_id), private_key)
        _ok(f"Auth.AppAuth created for app_id={app_id}")
        return app_auth
    except Exception as e:
        _fail("Auth.AppAuth failed", e)
        return None


def check_installation_auth(app_auth: object, installation_id: int) -> object | None:
    _section("4. AppInstallationAuth — installation token exchange")
    try:
        from github import Auth
        inst_auth = Auth.AppInstallationAuth(app_auth, installation_id)
        _ok(f"Auth.AppInstallationAuth created for installation_id={installation_id}")
        return inst_auth
    except Exception as e:
        _fail("Auth.AppInstallationAuth failed", e)
        return None


def check_api_call(inst_auth: object) -> object | None:
    _section("5. API connectivity — get authenticated app")
    try:
        from github import Github
        g = Github(auth=inst_auth)
        # get_installation fetches the installation metadata — confirms token works
        rate = g.get_rate_limit()
        _ok(f"API call succeeded — core rate limit: {rate.core.remaining}/{rate.core.limit}")
        return g
    except Exception as e:
        _fail("API call failed", e)
        return None


def check_repo_access(g: object, repo_name: str) -> None:
    _section(f"6. Repo access — {repo_name}")
    try:
        repo = g.get_repo(repo_name)
        _ok(f"Repo found: {repo.full_name}")
        _info(f"Default branch : {repo.default_branch}")
        _info(f"Private        : {repo.private}")

        perms = repo.permissions
        if perms:
            _info(f"push={perms.push}  admin={perms.admin}  pull={perms.pull}")
            if perms.push:
                _ok("Push access confirmed")
            else:
                _fail("No push access — check app installation permissions")
        else:
            _info("(permissions not returned by API)")
    except Exception as e:
        _fail(f"Cannot access repo '{repo_name}'", e)


def main() -> None:
    repo_name = sys.argv[1] if len(sys.argv) > 1 else None
    print("\n=== GitHub App Auth Diagnostic (PyGithub 2.x) ===")

    s = get_settings()
    settings_ok = check_settings()
    if not settings_ok:
        print("\n\033[31mFix missing settings above before continuing.\033[0m\n")
        sys.exit(1)

    key_bytes = check_private_key(s.github_private_key_path)
    if key_bytes is None:
        sys.exit(1)

    private_key = key_bytes.decode("utf-8")

    app_auth = check_app_auth(s.github_app_id, private_key)
    if app_auth is None:
        sys.exit(1)

    inst_auth = check_installation_auth(app_auth, s.github_installation_id)
    if inst_auth is None:
        sys.exit(1)

    g = check_api_call(inst_auth)
    if g is None:
        sys.exit(1)

    if repo_name:
        check_repo_access(g, repo_name)
    else:
        print("\n     (pass owner/repo as an argument to also check repo access)")

    print("\n\033[32mAll checks passed.\033[0m\n")


if __name__ == "__main__":
    main()
