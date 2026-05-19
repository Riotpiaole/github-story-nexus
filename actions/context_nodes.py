import logging
import os
import re
import subprocess
from pathlib import Path

from langchain_core.messages import HumanMessage

from config import llm
from state import AgentState
from tools.ast_grep import search_functions

log = logging.getLogger(__name__)

_SKILLS_PATTERN = re.compile(r'^(\w+):\s*["\']?([^"\'#\n]+?)["\']?\s*$')
_IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}
_NOISE_TOKENS = re.compile(r'\b(__pycache__|\.pyc|\.DS_Store|Thumbs\.db)\b')

_PRUNE_LIMIT = 10
_README_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_README_FALLBACK_CHARS = 2000
_DEPENDENCY_MAX_LINES = 80
_ENTRY_POINT_MAX_LINES = 60
_FILE_TREE_MAX_LINES = 100

_README_SUMMARIZE_PROMPT = (
    "Summarise the following README in 150 words or fewer. "
    "Focus on: what the project does, who it is for, and its main features. "
    "Output plain prose only.\n\n{readme}"
)

# Candidate dependency manifests, checked in priority order per language family
_DEPENDENCY_FILES: list[str] = [
    "requirements.txt", "pyproject.toml", "setup.py",  # Python
    "package.json",                                      # JS/TS
    "go.mod",                                            # Go
    "Cargo.toml",                                        # Rust
    "pom.xml", "build.gradle",                          # Java
    "Gemfile",                                           # Ruby
]

# Candidate main entry points, checked in order
_ENTRY_POINTS: list[str] = [
    "main.py", "app.py", "server.py", "index.py",       # Python
    "index.js", "index.ts", "server.js", "app.js",      # JS/TS
    "main.go",                                           # Go
    "src/main.rs",                                       # Rust
    "Main.java",                                         # Java
]

_SUMMARIZE_PROMPT = """\
You are a senior software engineer analysing a codebase to brief an AI coding agent.

Using ALL sections below, produce a structured summary with these four headings:

## Purpose
One or two sentences on what this project does (derived from the README).

## Dependencies & Libraries
Bullet list of the key third-party libraries found in the dependency manifest and \
what each one is used for in this project.

## Main Loop / Entry Point
Describe the program's main loop or top-level execution flow (derived from the entry \
point file). Identify the framework, event loop, or scheduler driving the process.

## Required Skillset
Bullet list of the concrete technical skills an engineer needs to contribute to this \
codebase (languages, frameworks, tools, paradigms). Be specific — name versions where \
visible (e.g. "Python 3.12", "LangGraph 0.2", "pytest").

Keep the whole response under 300 words. Do NOT repeat raw file contents.

---
{readme}

---
{dependencies}

---
{entry_point}

---
{context}
"""


def repo_evaluation(state: AgentState) -> dict:
    """Validates the local repo: is a git repo, remote matches repo_name, working directory is clean."""
    repo_path = Path(state["local_repo_path"])

    if not (repo_path / ".git").exists():
        raise ValueError(f"Not a git repository: {repo_path}")

    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, cwd=repo_path,
    )
    if result.returncode != 0:
        raise ValueError("No remote 'origin' configured.")

    remote_url = result.stdout.strip()
    if state["repo_name"] not in remote_url:
        log.error(
            "Remote URL '%s' does not match provided --repo '%s'.",
            remote_url, state["repo_name"],
        )
        return {"status": "repo_mismatch"}

    dirty = subprocess.run(
        ["git", "diff-index", "--quiet", "HEAD", "--"],
        cwd=repo_path,
    )
    if dirty.returncode != 0:
        raise ValueError("Working directory has uncommitted changes — stash or commit before running.")

    log.info("Repo validation passed for %s", state["repo_name"])
    return {}


def load_skills(state: AgentState) -> dict:
    """Reads and parses skills.sh from the repo root into a dict."""
    skills_path = Path(state["local_repo_path"]) / "skills.sh"
    skills: dict = {}

    if not skills_path.exists():
        log.warning("skills.sh not found in %s — proceeding without project skills.", state["local_repo_path"])
        return {"skills": skills}

    for line in _read_text(skills_path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("export ") or line == "#!/bin/bash":
            continue
        match = _SKILLS_PATTERN.match(line)
        if match:
            skills[match.group(1)] = match.group(2).strip()

    log.info("Skills loaded: %s", skills)
    return {"skills": skills}


def _read_text(path: Path) -> str:
    """Read a file as UTF-8, falling back to latin-1 so no bytes are silently dropped."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        log.debug("UTF-8 decode failed for '%s', retrying with latin-1.", path)
        return path.read_text(encoding="latin-1")
    except PermissionError:
        log.warning("Permission denied reading '%s'.", path)
        return ""
    except OSError as e:
        log.warning("Could not read '%s': %s", path, e)
        return ""


def _read_readme(repo_path: Path) -> str:
    """Read README, warn if over 10 MB, then summarise to 150 words via Claude."""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        candidate = repo_path / name
        if not candidate.exists():
            continue

        try:
            size = candidate.stat().st_size
        except OSError as e:
            log.warning("Could not stat README '%s': %s", name, e)
            continue

        if size > _README_MAX_BYTES:
            log.warning("README '%s' is %.1f MB — large file, summarising anyway.", name, size / 1024 / 1024)

        raw = _read_text(candidate)
        if not raw:
            continue

        try:
            log.info("Summarising README '%s' (%.1f KB)...", name, size / 1024)
            prompt = _README_SUMMARIZE_PROMPT.format(readme=raw)
            response = llm.invoke([HumanMessage(content=prompt)])
            return response.content.strip()
        except Exception as e:
            log.warning("LLM failed to summarise README '%s': %s", name, e)
            return raw[:_README_FALLBACK_CHARS]

    return "(no README found)"


def _read_dependencies(repo_path: Path) -> str:
    """Return the contents of the first dependency manifest found, capped at 80 lines."""
    for name in _DEPENDENCY_FILES:
        candidate = repo_path / name
        if not candidate.exists():
            continue
        content = _read_text(candidate)
        if content:
            return f"### {name}\n" + "\n".join(content.splitlines()[:_DEPENDENCY_MAX_LINES])
    return "(no dependency manifest found)"


def _read_entry_point(repo_path: Path) -> str:
    """Return the first 60 lines of the project's main entry point file."""
    for name in _ENTRY_POINTS:
        candidate = repo_path / name
        if not candidate.exists():
            continue
        content = _read_text(candidate)
        if content:
            return f"### {name}\n" + "\n".join(content.splitlines()[:_ENTRY_POINT_MAX_LINES])
    return "(no entry point found)"


def _token_filter(lines: list[str]) -> list[str]:
    """Remove noise lines: blank, pure punctuation, and known junk filenames."""
    return [
        ln for ln in lines
        if ln.strip() and not _NOISE_TOKENS.search(ln)
    ]


def _prune(func_lines: list[str]) -> list[str]:
    """Keep only the most recent _PRUNE_LIMIT function entries."""
    return func_lines[-_PRUNE_LIMIT:]


def _summarize(raw_context: str, readme: str, dependencies: str, entry_point: str) -> str:
    """Use Claude to produce a structured 300-word project summary."""
    prompt = _SUMMARIZE_PROMPT.format(
        readme=f"## README\n{readme}",
        dependencies=f"## Dependency Manifest\n{dependencies}",
        entry_point=f"## Entry Point\n{entry_point}",
        context=f"## File Structure & Function Index\n{raw_context}",
    )
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content.strip()


def load_context(state: AgentState) -> dict:
    """Builds and compresses project context via token filtering, pruning, and LLM summarization."""
    repo_path = Path(state["local_repo_path"])
    language = state.get("skills", {}).get("language", "python")

    # --- Gather README, dependencies, and entry point ---
    readme = _read_readme(repo_path)
    dependencies = _read_dependencies(repo_path)
    entry_point = _read_entry_point(repo_path)

    # --- Raw file tree ---
    tree_lines: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_DIRS)
        rel = Path(root).relative_to(repo_path)
        depth = len(rel.parts)
        if str(rel) != ".":
            tree_lines.append("  " * depth + str(rel.name) + "/")
        for f in sorted(files):
            tree_lines.append("  " * (depth + 1) + f)
        if len(tree_lines) >= _FILE_TREE_MAX_LINES:
            tree_lines.append("  ... (truncated)")
            break

    # --- Raw function index ---
    func_lines: list[str] = []
    search_result = search_functions("", repo_path=str(repo_path), language=language)
    if search_result["success"]:
        for match in search_result["results"]:
            first_line = (match["text"] or "").splitlines()[0]
            func_lines.append(f"  {match['file']}:{match['line']} — {first_line}")

    # 1. Token filter — strip noise from both lists
    tree_lines = _token_filter(tree_lines)
    func_lines = _token_filter(func_lines)

    # 2. Prune — keep only the latest _PRUNE_LIMIT functions
    func_lines = _prune(func_lines)

    raw_context = "## File Structure\n" + "\n".join(tree_lines)
    if func_lines:
        raw_context += "\n\n## Function Index (latest 10)\n" + "\n".join(func_lines)

    # 3. Summarize — LLM produces structured summary from all four sources
    compressed = _summarize(raw_context, readme, dependencies, entry_point)

    log.info("Context compressed: %d chars → %d chars", len(raw_context), len(compressed))
    return {"project_context": compressed}


def load_project_context(state: AgentState) -> dict:
    """LangGraph node: runs repo_evaluation → load_skills → load_context in sequence."""
    result: dict = {}

    result.update(repo_evaluation(state))
    if result.get("status") == "repo_mismatch":
        return result

    interim = {**state, **result}
    result.update(load_skills(interim))

    interim = {**interim, **result}
    result.update(load_context(interim))

    return result
