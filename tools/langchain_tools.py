"""LangChain tool wrappers for codebase search and file modification.

Search tools let the LLM understand existing patterns; file tools let it make
targeted in-place edits rather than generating a standalone code snippet.
"""

from pathlib import Path
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


@tool
def read_file(path: str, repo_path: str) -> str:
    """Read the full contents of a file in the repository.

    Always read a file before editing it to understand its current state.

    Args:
        path: Repo-relative path (e.g. "src/utils.py")
        repo_path: Absolute path to the repository root

    Returns:
        File contents, or an error message if the file does not exist.
    """
    target = Path(repo_path) / path
    if not target.exists():
        return f"File not found: {path}"
    return target.read_text()


@tool
def list_directory(path: str, repo_path: str) -> str:
    """List files and subdirectories at a path inside the repository.

    Use this to explore project structure before deciding which files to edit.

    Args:
        path: Repo-relative directory path (use "." for the root)
        repo_path: Absolute path to the repository root

    Returns:
        Newline-separated list of entries tagged [dir] or [file].
    """
    target = Path(repo_path) / path
    if not target.exists():
        return f"Path not found: {path}"
    if not target.is_dir():
        return f"Not a directory: {path}"
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    lines = [f"{'[dir] ' if e.is_dir() else '[file]'} {e.name}" for e in entries]
    return "\n".join(lines) if lines else "(empty directory)"


@tool
def write_file(path: str, content: str, repo_path: str) -> str:
    """Write or overwrite a file in the repository.

    Use for new files or when replacing the entire content of an existing file.
    For surgical edits, prefer str_replace_in_file.

    Args:
        path: Repo-relative path (e.g. "src/new_module.py")
        content: Full file content to write
        repo_path: Absolute path to the repository root

    Returns:
        Confirmation message with the resolved path.
    """
    target = Path(repo_path) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Written: {path}"


@tool
def str_replace_in_file(path: str, old_str: str, new_str: str, repo_path: str) -> str:
    """Replace an exact substring in a file with new content.

    The old_str must match exactly (including whitespace and indentation) and must
    appear exactly once in the file.

    Args:
        path: Repo-relative path (e.g. "src/utils.py")
        old_str: Exact string to find (must be unique in the file)
        new_str: Replacement string
        repo_path: Absolute path to the repository root

    Returns:
        Confirmation, or an error if the string was not found or is ambiguous.
    """
    target = Path(repo_path) / path
    if not target.exists():
        return f"File not found: {path}"
    original = target.read_text()
    count = original.count(old_str)
    if count == 0:
        return f"str_replace_in_file failed: old_str not found in {path}"
    if count > 1:
        return f"str_replace_in_file failed: old_str matches {count} times in {path} — use more context to make it unique"
    target.write_text(original.replace(old_str, new_str, 1))
    return f"Replaced 1 occurrence in {path}"


def get_code_search_tools():
    """Returns read-only search tools (used for non-coder nodes)."""
    return [find_functions, search_code_patterns]


def get_coder_tools():
    """Returns the full ReAct tool set: search + file read/write/edit."""
    return [find_functions, search_code_patterns, read_file, list_directory, write_file, str_replace_in_file]
