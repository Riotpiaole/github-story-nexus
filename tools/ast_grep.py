import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


def _run_ast_grep(pattern: str, repo_path: str, language: str) -> dict:
    try:
        result = subprocess.run(
            ["ast-grep", "--pattern", pattern, "--language", language, "--json", str(repo_path)],
            capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Search timed out after {DEFAULT_TIMEOUT}s"}
    except FileNotFoundError:
        return {"success": False, "error": "ast-grep not found. Install with: npm install -g ast-grep"}
    except Exception as e:
        log.exception("Error during ast-grep search")
        return {"success": False, "error": str(e)}

    if result.returncode != 0:
        return {"success": False, "error": result.stderr or "ast-grep search failed"}

    try:
        output = json.loads(result.stdout) if result.stdout else {"matches": []}
    except json.JSONDecodeError:
        return {"success": False, "error": "Failed to parse ast-grep JSON output"}

    return {
        "success": True,
        "results": [
            {
                "file": m.get("file"),
                "line": m.get("range", {}).get("start", {}).get("line"),
                "text": m.get("text", "")[:100],
            }
            for m in output.get("matches", [])
        ],
    }


def search_functions(pattern: str, repo_path: str, language: str = "golang") -> dict:
    """Search for function definitions matching a pattern using ast-grep."""
    return _run_ast_grep(f"function $_{pattern}", repo_path, language)


def search_patterns(pattern: str, repo_path: str, language: str = "python") -> dict:
    """Search for code matching an arbitrary AST pattern using ast-grep."""
    return _run_ast_grep(pattern, repo_path, language)
