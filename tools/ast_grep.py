import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


def search_functions(pattern: str, repo_path: str, language: str = "python") -> dict:
    """Search for function definitions matching a pattern using ast-grep.

    Args:
        pattern: AST pattern to search for (e.g., "function" or regex pattern)
        repo_path: Path to repository to search in
        language: Programming language (default: "python")

    Returns:
        Dict with 'success' bool and 'results' list containing matches with file paths and line numbers
    """
    try:
        cmd = [
            "ast-grep",
            "--pattern",
            f"function $_{pattern}",
            "--language",
            language,
            "--json",
            str(repo_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT)

        if result.returncode == 0:
            try:
                output = json.loads(result.stdout) if result.stdout else {"matches": []}
                matches = output.get("matches", [])
                formatted_results = []
                for match in matches:
                    formatted_results.append({
                        "file": match.get("file"),
                        "line": match.get("range", {}).get("start", {}).get("line"),
                        "text": match.get("text", "")[:100],  # First 100 chars
                    })
                return {"success": True, "results": formatted_results}
            except json.JSONDecodeError:
                return {"success": False, "error": "Failed to parse ast-grep JSON output"}
        else:
            return {"success": False, "error": result.stderr or "ast-grep search failed"}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Search timed out after {DEFAULT_TIMEOUT}s"}
    except FileNotFoundError:
        return {
            "success": False,
            "error": "ast-grep not found. Install with: npm install -g ast-grep"
        }
    except Exception as e:
        log.exception("Error during ast-grep search")
        return {"success": False, "error": str(e)}


def search_patterns(pattern: str, repo_path: str, language: str = "python") -> dict:
    """Search for code matching an arbitrary AST pattern using ast-grep.

    Args:
        pattern: AST pattern to search for (ast-grep syntax)
        repo_path: Path to repository to search in
        language: Programming language (default: "python")

    Returns:
        Dict with 'success' bool and 'results' list containing matches
    """
    try:
        cmd = [
            "ast-grep",
            "--pattern",
            pattern,
            "--language",
            language,
            "--json",
            str(repo_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT)

        if result.returncode == 0:
            try:
                output = json.loads(result.stdout) if result.stdout else {"matches": []}
                matches = output.get("matches", [])
                formatted_results = []
                for match in matches:
                    formatted_results.append({
                        "file": match.get("file"),
                        "line": match.get("range", {}).get("start", {}).get("line"),
                        "text": match.get("text", "")[:100],
                    })
                return {"success": True, "results": formatted_results}
            except json.JSONDecodeError:
                return {"success": False, "error": "Failed to parse ast-grep JSON output"}
        else:
            return {"success": False, "error": result.stderr or "ast-grep search failed"}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Search timed out after {DEFAULT_TIMEOUT}s"}
    except FileNotFoundError:
        return {
            "success": False,
            "error": "ast-grep not found. Install with: npm install -g ast-grep"
        }
    except Exception as e:
        log.exception("Error during ast-grep pattern search")
        return {"success": False, "error": str(e)}
