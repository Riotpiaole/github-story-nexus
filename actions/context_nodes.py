import json
import logging
import os
import re
import subprocess
from pathlib import Path

from langchain_core.messages import HumanMessage

from cache import get_cache
from state import AgentState
from tools.ast_grep import search_functions
from tools.github import fetch_issue_from_github

from config import llm

log = logging.getLogger(__name__)

_CACHE_KEY = "ctx:{user_id}:{safe_repo}"

_SYMBOL_STRIP = re.compile(r'[#*`>_\-|\\]')
_WHITESPACE_COLLAPSE = re.compile(r'\s+')

_SKILLS_PATTERN = re.compile(r'^(\w+):\s*["\']?([^"\'#\n]+?)["\']?\s*$')
_IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}
_NOISE_TOKENS = re.compile(r'\b(__pycache__|\.pyc|\.DS_Store|Thumbs\.db)\b')

_MISSING_README = "(no README found)"
_MISSING_DEPENDENCY_MANIFEST = "(no dependency manifest found)"
_MISSING_ENTRY_POINT = "(no entry point found)"

_PRUNE_LIMIT = 10
_README_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_README_FALLBACK_CHARS = 2000
_DEPENDENCY_MAX_LINES = 80
_ENTRY_POINT_MAX_LINES = 60
_FILE_TREE_MAX_LINES = 100

_README_SUMMARIZE_PROMPT = """\
You are a senior software engineer acting as a planning agent.

Given the project README and a GitHub issue, analyze both and return ONLY a \
raw JSON object — no markdown fences, no extra text — with exactly these two keys:

"readMeContent": A concise summary (150 words or fewer) covering:
  - What the project does and its main purpose
  - Key architectural components or technologies used
  - How the issue relates to the project and what area of the codebase it likely touches

"language": The single ast-grep language identifier required to resolve the issue.
  Choose from: python, javascript, typescript, go, rust, java, ruby
  Base this on the primary implementation language visible in the README (build tools,
  import examples, file extensions mentioned) and the nature of the issue.

## README
{readme}

## GitHub Issue
{issue_content}
"""

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
    """Discovers and installs agent skills via find-skills CLI using the compressed project context."""
    context = state.get("project_context", "")
    if not context:
        log.warning("No project context available — skipping skill discovery.")
        return {"skills": {}}

    query = _extract_skillset_query(context)

    try:
        search = subprocess.run(
            ["npx", "--yes", "skills", "find", query],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise RuntimeError("npx not found — install Node.js to enable skill discovery.")

    if search.returncode != 0:
        log.warning("skills find failed: %s", search.stderr.strip())
        return {"skills": {}}

    skills = _parse_skills_output(search.stdout)

    if not skills:
        log.warning("find-skills returned no matching skills for the project context.")
        return {"skills": {}}

    log.info("Skills discovered: %s", list(skills.keys()))

    for name, source in skills.items():
        _install_skill(name, source)

    return {"skills": skills}


def _extract_skillset_query(context: str) -> str:
    cleaned = _SYMBOL_STRIP.sub(' ', context)
    return _WHITESPACE_COLLAPSE.sub(' ', cleaned).strip()


def _parse_skills_output(output: str) -> dict:
    """Parse `npx skills find` stdout into {skill_name: source_url}."""
    skills: dict = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SKILLS_PATTERN.match(line)
        if m:
            skills[m.group(1)] = m.group(2).strip()
    return skills


def _install_skill(name: str, source: str) -> None:
    result = subprocess.run(
        ["npx", "--yes", "skills", "add", source, "--skill", name],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        log.info("Installed skill '%s' from %s", name, source)
    else:
        log.warning("Failed to install skill '%s': %s", name, result.stderr.strip())


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


def _read_readme(repo_path: Path, issue_content: str) -> dict:
    """Read README and summarise it together with the issue in one LLM call.

    LLM calls: 1
    Returns:
        {"readMeContent": "<summary>", "language": "<ast-grep language id>"}
    """
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
            prompt = _README_SUMMARIZE_PROMPT.format(
                readme=raw,
                issue_content=issue_content,
            )
            response = llm.invoke([HumanMessage(content=prompt)])
            parsed = json.loads(response.content.strip())
            log.info("Language inferred from README: %s", parsed.get("language"))
            return parsed
        except json.JSONDecodeError as e:
            log.warning("LLM returned non-JSON for README '%s': %s", name, e)
            return {"readMeContent": raw[:_README_FALLBACK_CHARS], "language": "python"}
        except Exception as e:
            log.warning("LLM failed to summarise README '%s': %s", name, e)
            return {"readMeContent": raw[:_README_FALLBACK_CHARS], "language": "python"}

    return _MISSING_README


def _read_dependencies(repo_path: Path) -> str:
    """Return the contents of the first dependency manifest found, capped at 80 lines."""
    for name in _DEPENDENCY_FILES:
        candidate = repo_path / name
        if not candidate.exists():
            continue
        content = _read_text(candidate)
        if content:
            return f"### {name}\n" + "\n".join(content.splitlines()[:_DEPENDENCY_MAX_LINES])
    return _MISSING_DEPENDENCY_MANIFEST


def _read_entry_point(repo_path: Path) -> str:
    """Return the first 60 lines of the project's main entry point file."""
    for name in _ENTRY_POINTS:
        candidate = repo_path / name
        if not candidate.exists():
            continue
        content = _read_text(candidate)
        if content:
            return f"### {name}\n" + "\n".join(content.splitlines()[:_ENTRY_POINT_MAX_LINES])
    return _MISSING_ENTRY_POINT


def _token_filter(lines: list[str]) -> list[str]:
    """Remove noise lines: blank, pure punctuation, and known junk filenames."""
    return [
        ln for ln in lines
        if ln.strip() and not _NOISE_TOKENS.search(ln)
    ]


def _prune(func_lines: list[str]) -> list[str]:
    """Keep only the most recent _PRUNE_LIMIT function entries."""
    return func_lines[-_PRUNE_LIMIT:]


def _summarize(
    raw_context: str,
    readme: str,
    dependencies: str,
    entry_point: str,
) -> dict:
    """Use Claude to produce a structured 300-word project summary.

    LLM calls: 1
    Returns:
        {"readme": "<structured summary>", "language": "<ast-grep language id>"}
    """
    prompt = _SUMMARIZE_PROMPT.format(
        readme=f"## README\n{readme}",
        dependencies=f"## Dependency Manifest\n{dependencies}",
        entry_point=f"## Entry Point\n{entry_point}",
        context=f"## File Structure & Function Index\n{raw_context}",
    )
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content.strip()


def load_context(state: AgentState) -> dict:
    """Builds and compresses project context via token filtering, pruning, and LLM summarization.

    LLM calls: 2 (_read_readme x 1, _summarize x 1)
    """
    repo_path = Path(state["local_repo_path"])

    # --- Fetch GitHub issue before building context ---
    log.info("Fetching GitHub issue #%d from %s...", state["issue_number"], state["repo_name"])
    api_response = fetch_issue_from_github(state["repo_name"], state["issue_number"])
    issue_content = f"Github Issue: Title: {api_response['title']}\nBody: {api_response['body']}"
    log.info("Issue fetched: %s", api_response["title"])
    
    # --- Gather README (+ language inference), dependencies, and entry point ---
    readme = _read_readme(repo_path, issue_content)
    if readme == _MISSING_README:
        raise FileNotFoundError(
            f"No README found in '{repo_path}'. "
            "Add a README.md (or README.rst / README.txt) to the repository root so the agent "
            "can summarise the project and infer the target language."
        )
    else:
        language = readme["language"]

        
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


def _make_context_key(user_id: str, repo_name: str) -> str:
    """Build the context cache key.

    Current scheme: user_id + repo_name.
    Extend here when finer-grained invalidation is needed
    (e.g. append commit SHA for per-commit caching).
    """
    safe_repo = repo_name.replace("/", "_")
    return _CACHE_KEY.format(
        user_id=user_id,
        safe_repo=safe_repo,
    )


def load_project_context(state: AgentState) -> dict:
    """LangGraph node: repo_evaluation → fetch issue → (L1/L2 cache check) → load_context → load_skills.

    LLM calls: 2 on cache miss (via load_context), 0 on cache hit
    """
    result: dict = {}

    result.update(repo_evaluation(state))
    if result.get("status") == "repo_mismatch":
        return result

    interim = {**state, **result}

    # --- Context cache: Redis L1 (60 s TTL) → PostgreSQL L2 (persistent) ---
    cache_key = _make_context_key(state["user_id"], state["repo_name"])
    cache = get_cache()

    cached = cache.get(cache_key)
    if cached is not None:
        log.info("Context cache hit repo=%s key=%s", state["repo_name"], cache_key)
        result["project_context"] = cached
    else:
        result.update(load_context(interim))
        cache.set(cache_key, result["project_context"])
        log.info("Context cached repo=%s key=%s", state["repo_name"], cache_key)

    interim = {**interim, **result}
    result.update(load_skills(interim))

    return result
