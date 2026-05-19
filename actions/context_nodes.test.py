"""
Integration test for load_project_context.

Loads the local .env, then calls the node against a real local repo clone
with no mocks so every sub-step (repo validation, skills, LLM summarisation)
runs for real.

Usage:
    python actions/context_nodes.test.py
    pytest actions/context_nodes.test.py -v
"""
import sys
from pathlib import Path

# Must come before any project imports: add repo root to sys.path and load .env
# so that `state`, `tools`, and `config` (with ANTHROPIC_API_KEY) are all resolvable.
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from context_nodes import load_project_context  # noqa: E402


# ---------------------------------------------------------------------------
# Target repo
# ---------------------------------------------------------------------------

_REPO_NAME = "Riotpiaole/story-nexus-test"
_LOCAL_PATH = "/Users/rockliang/workplace/simple_calculator"


def _build_state() -> dict:
    return {
        "repo_name": _REPO_NAME,
        "issue_number": 0,
        "local_repo_path": _LOCAL_PATH,
        "base_branch": "main",
        "skills": {},
        "project_context": "",
        "issue_details": "",
        "code_snippet": "",
        "test_code": "",
        "test_results": "",
        "retry_count": 0,
        "max_retries": 3,
        "status": "",
        "pr_url": "",
    }


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

def test_load_project_context_integration():
    state = _build_state()
    print(f"\n[integration] calling load_project_context for '{_REPO_NAME}' at {_LOCAL_PATH}")

    result = load_project_context(state)

    assert result.get("status") != "repo_mismatch", (
        f"repo_mismatch: '{_REPO_NAME}' did not match the remote origin at '{_LOCAL_PATH}'. "
        "Run `git -C <path> remote get-url origin` to verify."
    )
    assert "project_context" in result, "Expected 'project_context' key in result"
    assert len(result["project_context"]) > 100, "project_context is suspiciously short"

    # print("\n--- skills ---")
    # for k, v in result.get("skills", {}).items():
    #     print(f"  {k}: {v}")

    # print("\n--- project_context (first 500 chars) ---")
    # print(result["project_context"][:500])


if __name__ == "__main__":
    test_load_project_context_integration()
    print("\nIntegration test passed.")
