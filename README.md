# story-pr-agent

A **generic issue-to-PR generator** that automatically reads GitHub issues, generates solutions tailored to the project's language and architecture, tests them, and opens pull requests. Works with any repository by detecting project skills and patterns.

## Overview

This tool transforms GitHub issues into complete pull requests through an intelligent, project-aware workflow:

```
load_project_context ──(repo mismatch)──────────────────────────────► fail_state
        │
        ▼
   read_issue → plan → code → test ──(pass)──────────────────────► create_pr
                         ▲     │
                         │     └──(fail)──► tester ──(APPROVED)──► create_pr
                         │                    │
                         └──(NEEDS_WORK, retry remaining)
                                              │
                                       (max iterations)
                                              ▼
                                          fail_state
```

`load_project_context` bundles repo validation, skills loading, and context generation. If the `--repo` flag does not match the remote origin, the workflow routes immediately to `fail_state` without proceeding further.

### Workflow Stages

1. **load_project_context** — Validates the repo, loads skills, and generates a compressed project summary (cached in Redis → PostgreSQL). Fails immediately if `--repo` does not match the remote origin.
2. **read_issue** — Fetches issue title + body from GitHub
3. **plan** — Single LLM call producing a structured implementation plan from the issue and cached project context; no codebase re-exploration
4. **code** — ReAct loop: Claude explores the repo with `list_directory`/`read_file`, locates targets with ast-grep search tools, then edits files in-place via `str_replace_in_file`/`write_file`; outputs only unit tests in its final response
5. **test** — Runs solution + unit tests in an isolated Docker sandbox using the project's detected test runner
6. **tester** — On failure: analyzes execution output and produces a structured verdict — `APPROVED` (solution is correct, early-terminate to PR) or `NEEDS_WORK` (feedback for the coder to iterate on)
7. **create_pr** — Creates branch, commits, pushes, opens PR

## Quick Start

### Usage

```bash
python main.py --repo owner/repo --issue 42 --path /path/to/repo [--base main]
```

### Key Feature: Dynamic Skills Discovery

The agent automatically discovers and installs the right skills for your project using the [find-skills](https://www.skills.sh/vercel-labs/skills/find-skills) CLI. After building a compressed summary of the repository (README, dependencies, entry point, file tree), it queries the skills.sh registry to find matching agent skills and installs them — no manual configuration needed.

This enables:
- Language-specific code generation (Python, JavaScript, Go, Rust, Java, etc.)
- Framework-aware patterns (Django ORM, FastAPI, Express, etc.)
- Correct test syntax (pytest, jest, unittest, mocha, etc.)
- Proper package management (pip, npm, cargo, etc.)

## Requirements

- Python 3.12+
- Docker (for sandboxed testing)
- GitHub credentials (PAT or App)
- Redis (for context caching)
- PostgreSQL with pgvector (optional, for semantic search)
- `ast-grep` CLI (for code analysis)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/story-pr-agent.git
cd story-pr-agent
```

### 2. Install Python dependencies

```bash
uv add -r requirements.txt
```

### 3. Build the Docker test runner image

```bash
docker build -t story-pr-runner -f Dockerfile.runner .
```

### 4. Install ast-grep

```bash
npm install -g @ast-grep/cli
```

### 5. Start Redis

```bash
redis-server
# Or via Docker:
docker run -d -p 6379:6379 redis:latest
```

### 6. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...

# GitHub auth — choose one option:
GITHUB_TOKEN=ghp_...
# OR
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY_PATH=/path/to/private-key.pem
GITHUB_INSTALLATION_ID=78901234

# Storage
REDIS_URL=redis://localhost:6379
POSTGRES_VEC_URL=postgresql://user:password@localhost/vectordb  # Optional

# Agent tuning
MAX_RETRIES=3
LLM_MODEL=claude-sonnet-4-6
BASE_BRANCH=main
```

## Project Configuration: skills.sh

To enable smart project detection and generation, add a `skills.sh` file to your repository root:

```bash
#!/bin/bash
# skills.sh - Project skills and specifications

# Core language
language: "python"
python_version: "3.11"

# Framework/Libraries
framework: "django"
framework_version: "4.2"

# Testing
test_runner: "pytest"
test_directory: "tests/"

# Package management
package_manager: "pip"
requirements_file: "requirements.txt"

# Code quality
code_formatter: "black"
type_checker: "mypy"

# Special capabilities
async_support: "true"
orm_used: "django-orm"
```

### Supported Configurations

The agent understands:
- **Languages**: python, javascript, typescript, go, rust, java, c++, php, ruby
- **Frameworks**: django, fastapi, flask, express, nextjs, nuxt, echo, actix, spring, laravel, rails
- **Test runners**: pytest, jest, unittest, mocha, go test, cargo test, junit
- **Package managers**: pip, npm, yarn, pnpm, cargo, go modules, maven, bundler

## Running

### CLI

```bash
python main.py \
  --repo owner/repo \
  --issue 42 \
  --path /path/to/local/clone \
  --base main
```

| Flag | Required | Description |
|------|----------|-------------|
| `--repo` | yes | `owner/repo` format |
| `--issue` | yes | Issue number |
| `--path` | yes | Path to local clone of repo |
| `--base` | no | Base branch (default: `main`) |

### Via Flask REST API

```bash
flask --app app run --host 0.0.0.0 --port 8000
```

Start a run:

```bash
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"repo": "owner/repo", "issue": 42, "path": "/path/to/local/clone"}'
# → {"run_id": "<uuid>", "status": "running"}
```

Poll for the result:

```bash
curl http://localhost:8000/run/<uuid>
# → {"run_id": "...", "status": "done", "agent_status": "pr_created", "pr_url": "https://..."}
```

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/run` | `POST` | Start a new agent run; returns `run_id` immediately |
| `/run/<run_id>` | `GET` | Poll status: `running`, `done`, or `error` |
| `/health` | `GET` | Liveness check |

Because the workflow involves multiple LLM calls and git operations (30–120 s), the endpoint returns `202 Accepted` with a `run_id` and runs the graph in a background thread.

### Via Webhook (For Automated Triggers)

```bash
uvicorn webhook:app --host 0.0.0.0 --port 8000
```

Configure GitHub webhook:
- **Payload URL**: `https://your-server/webhook`
- **Content type**: `application/json`
- **Events**: Issues
- **Secret**: Set `GITHUB_WEBHOOK_SECRET` in `.env`

Trigger by labeling an issue with `generate-pr`

## How It Works

### Repo Validation

Before processing, the agent validates:
- ✓ Local path exists and is a git repository
- ✓ Remote origin is configured and accessible
- ✓ `--repo` matches the remote origin URL
- ✓ No uncommitted changes that would conflict with PR

If `--repo` does not match the remote origin, the workflow sets `status: repo_mismatch` and routes directly to `fail_state` — no skills loading, no context generation, no LLM calls. The CLI exits with code 1 and logs the conflicting repo and path.

### Skills Detection

After generating the compressed project context, the agent runs `npx skills find` with the full context text as the search query. Matching skills are then installed automatically via `npx skills add`.

This replaces the earlier static `skills.sh` approach — skills are now **discovered dynamically** from the project's actual content rather than declared manually.

Requirements:
- Node.js must be installed (`npx` available on `$PATH`)
- Internet access to reach the skills.sh registry

This enables **generic code generation** — the same agent can generate Python, JavaScript, Go, etc.

### Context Loading

The agent analyzes the project structure:
- File organization and directory layout
- Existing functions and classes (via ast-grep)
- Code patterns and conventions
- Import structure and dependencies
- Testing patterns and locations

This context is:
- **Cached in Redis** for fast retrieval on repeated runs
- **Compressed** to control LLM token usage
- **Injected into prompts** to guide code generation

### Planning

Before writing any code, the agent runs a single LLM call against the cached `project_context` and the GitHub issue to produce a structured implementation plan (approach selection, affected components, ordered steps, risk assessment). The planner never re-explores the codebase — the cached context is sufficient.

### Code Generation with Skills

The coder runs a **ReAct (Reason + Act)** loop — up to 10 tool iterations, 5000 output token cap — where it explores and edits the repository directly rather than generating a standalone snippet:

| Step | Tools used |
|------|-----------|
| Explore structure | `list_directory`, `read_file` |
| Locate targets | `find_functions`, `search_code_patterns` |
| Apply changes | `str_replace_in_file` (targeted edit), `write_file` (new file) |
| Finish | Emits only `### TESTS <unit test code>` |

Every `write_file` / `str_replace_in_file` call is tracked as a `modified_files` entry. After the loop, those files are read back and concatenated as `code_snippet` for the sandboxed executor.

On retry, the coder receives the tester's structured feedback. Because prior edits are already in the repo, it uses `str_replace_in_file` to fix specific problems rather than rewriting from scratch.

### Iterative Coder-Tester Loop

After each code generation:
1. Solution + unit tests are executed in an isolated Docker sandbox using the project's detected test runner (`skills.test_runner`)
2. On failure, the **tester** analyzes the execution output and emits a verdict:
   - `VERDICT: APPROVED` — solution satisfies the issue requirements (e.g. tests were wrong but code is correct); skips further iteration and goes straight to PR creation
   - `VERDICT: NEEDS_WORK` — provides root cause, solution issues, test issues, and concrete suggestions for the coder
3. The coder retries with the tester's feedback until tests pass, an APPROVED verdict is received, or `MAX_RETRIES` is exhausted
4. The tester is also capped at **5000 output tokens** per call

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | **Required** Anthropic API key |
| `GITHUB_TOKEN` | — | GitHub Personal Access Token (auth method A) |
| `GITHUB_APP_ID` | — | GitHub App ID (auth method B) |
| `GITHUB_PRIVATE_KEY_PATH` | — | Path to GitHub App private key |
| `GITHUB_INSTALLATION_ID` | — | GitHub App installation ID |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for webhook requests |
| `MAX_RETRIES` | `3` | Max full coder→tester iterations before giving up |
| `LLM_MODEL` | `claude-sonnet-4-6` | Claude model to use for all LLM calls |
| `BASE_BRANCH` | `main` | Default PR target branch |
| `REDIS_URL` | `redis://localhost:6379` | Redis — L1 context cache (60 s TTL) |
| `POSTGRES_VEC_URL` | `postgresql://…/vectordb` | PostgreSQL — L2 context cache (persistent fallback) |
| `LANGCHAIN_TRACING_V2` | `false` | Enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | — | LangSmith API key |
| `LANGCHAIN_PROJECT` | `story-pr-agent` | LangSmith project name |

## Project Structure

```
.
├── main.py                         # CLI entry point
├── app.py                          # Flask REST API server
├── webhook.py                      # FastAPI webhook server (GitHub events)
├── graph.py                        # LangGraph state machine
├── state.py                        # State schema & routing
├── config.py                       # Settings & initialization
├── Dockerfile.runner               # Test execution Docker image
├── requirements.txt                # Python dependencies
├── .env.example                    # Environment variable template
├── actions/
│   ├── git_nodes.py               # Git operations (read issue, create PR)
│   ├── llm_nodes.py               # LLM nodes: planner, coder, tester, test runner
│   ├── context_nodes.py           # Repo validation, skills loading, context gen
│   ├── planner_prompt.md          # Structured planning prompt (PLAN MODE)
│   ├── coder_prompt.md            # ReAct coder prompt (explore → edit → output tests)
│   └── tester_prompt.md           # Generic failure analysis prompt (APPROVED / NEEDS_WORK)
├── cache/
│   ├── __init__.py                # Public API: get_cache()
│   ├── _manager.py                # Two-level cache orchestration (L1 → L2)
│   ├── _redis.py                  # L1: Redis with 60 s TTL
│   └── _pg.py                     # L2: PostgreSQL persistent fallback
├── tools/
│   ├── executor.py                # Docker test execution
│   ├── github.py                  # GitHub API integration
│   ├── git_ops.py                 # Git CLI operations
│   ├── ast_grep.py                # ast-grep CLI wrapper
│   ├── langchain_tools.py         # LLM-callable tools: search + file read/write/edit
│   ├── storage.py                 # Redis + PostgreSQL abstraction
│   ├── context_generator.py       # Project context generation
│   └── context_compression.py     # Context compression strategies
└── README.md                       # This file
```

## Capabilities by Language

| Language | Framework | Test | Status |
|----------|-----------|------|--------|
| Python | Django, FastAPI, Flask | pytest, unittest | ✅ Full support |
| JavaScript | Express, Next.js | Jest, Mocha | ✅ Full support |
| Go | Gin, Echo | Go test | ✅ Full support |
| TypeScript | NestJS, Remix | Jest | ✅ Full support |
| Rust | Actix, Rocket | Cargo test | 🟡 Partial |
| Java | Spring | JUnit | 🟡 Partial |

Full support: Code generation + testing working reliably
Partial support: Code generation works, testing may need manual config

## Examples

### Example 1: Python Django Project

```bash
# Repository with skills.sh:
language: python
framework: django
test_runner: pytest

# Issue: "Add user authentication endpoint"
python main.py --repo myorg/myproject --issue 123 --path /path/to/myproject

# Result: Claude generates:
# - Django models for authentication
# - API endpoints with proper decorators
# - pytest tests matching project structure
# - PR with all changes
```

### Example 2: Node.js Express API

```bash
# Repository with skills.sh:
language: javascript
framework: express
test_runner: jest

# Issue: "Add rate limiting middleware"
python main.py --repo myorg/api --issue 456 --path /path/to/api

# Result: Claude generates:
# - Express middleware function
# - Jest tests with mocks
# - Integration into existing routes
# - PR ready to merge
```

## Troubleshooting

### Redis Connection Error
- Ensure Redis is running: `redis-cli ping`
- Check `REDIS_URL` in `.env`

### Docker Test Runner Not Found
- Build image: `docker build -t story-pr-runner -f Dockerfile.runner .`

### Repo Validation Fails
- **`--repo` mismatch**: The `--repo owner/repo` value must match the remote origin of the local clone at `--path`. Check with `git -C /path/to/repo remote get-url origin`.
- **Uncommitted changes**: Stash or commit all local changes before running (`git stash`).

### No Skills Discovered
- Ensure `npx` is available: `npx --version`
- Check internet access — the skills.sh registry must be reachable
- A warning is logged and the agent continues without skills if none are found

### LLM Generation Fails
- Check `ANTHROPIC_API_KEY` is valid
- Verify Claude model in `.env` is available
- Check token limits (Claude Sonnet has 200K context)

## Contributing

Contributions welcome! When adding support for new languages/frameworks:

1. Add language support to `context_generator.py`
2. Update `skills.sh` example in this README
3. Add to "Capabilities by Language" table
4. Test with example repository

## License

MIT

## Support

- 🐛 Issues: GitHub Issues
- 💬 Discussions: GitHub Discussions
- 📖 Docs: See README.md
