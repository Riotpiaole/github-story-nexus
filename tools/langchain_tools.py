"""LangChain tool wrappers for ast-grep code search functionality.

These tools allow the LLM to search the project's code structure to understand
existing patterns, functions, and conventions before generating new code.
"""

from typing import Optional
from langchain_core.tools import tool

from tools.ast_grep import search_functions, search_patterns


@tool
def find_functions(pattern: str, repo_path: str) -> str:
    """Search for function definitions in the codebase by name or pattern.

    Use this to understand existing function naming conventions, signatures, and patterns
    before generating new functions. This helps avoid duplicates and matches project style.

    Args:
        pattern: Function name or pattern to search for (e.g., "code_" or "test_")
        repo_path: Path to the repository to search in

    Returns:
        Formatted string with matching function definitions and their file locations
    """
    result = search_functions(pattern, repo_path, language="python")

    if not result["success"]:
        return f"Search failed: {result.get('error', 'Unknown error')}"

    results = result.get("results", [])
    if not results:
        return f"No functions matching '{pattern}' found in {repo_path}"

    formatted = f"Found {len(results)} function(s) matching '{pattern}':\n\n"
    for i, match in enumerate(results, 1):
        formatted += f"{i}. File: {match['file']} (line {match['line']})\n"
        formatted += f"   Code: {match['text']}\n\n"

    return formatted


@tool
def search_code_patterns(pattern: str, repo_path: str, language: str = "python") -> str:
    """Search the codebase for arbitrary code patterns using ast-grep syntax.

    Use this for more complex searches when looking for specific code structures
    (e.g., function calls, class definitions, import patterns).

    Args:
        pattern: AST pattern in ast-grep syntax (e.g., "function($type) { $$$ }")
        repo_path: Path to the repository to search in
        language: Programming language (default: "python")

    Returns:
        Formatted string with matching code patterns and their file locations
    """
    result = search_patterns(pattern, repo_path, language=language)

    if not result["success"]:
        return f"Search failed: {result.get('error', 'Unknown error')}"

    results = result.get("results", [])
    if not results:
        return f"No matches found for pattern '{pattern}' in {repo_path}"

    formatted = f"Found {len(results)} match(es) for pattern '{pattern}':\n\n"
    for i, match in enumerate(results, 1):
        formatted += f"{i}. File: {match['file']} (line {match['line']})\n"
        formatted += f"   Code: {match['text']}\n\n"

    return formatted


def get_code_search_tools():
    """Returns list of code search tools for binding to LLM."""
    return [find_functions, search_code_patterns]
