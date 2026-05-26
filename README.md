# story-pr-agent

A **generic issue-to-PR generator** that automatically reads GitHub issues, generates solutions tailored to the project's language and architecture, tests them, and opens pull requests. Works with any repository by detecting project skills and patterns.

## Overview

This tool transforms GitHub issues into complete pull requests through an intelligent, project-aware workflow:

```
preflight ──(fail)──────────────────────────────────────────────────────────► fail_state
        │
        ▼
load_project_context ──(repo mismatch)──────────────────────────────────────► fail_state
        │
        ▼
   read_issue → plan → execute_step ──(more steps)──► execute_step (loop)
                              │
                         (all done)
                              ▼
                           reflect → test ──(pass)──────────────────────────► create_pr
                                       │
                                       └──(fail)──► tester ──(APPROVED)─────► create_pr
                                                       │
                                              (NEEDS_WORK, retry remaining)
                                                       │
                                                    → plan (re-plan with feedback)
                                                       │
                                              (max retries exhausted)
                                                       ▼
                                                   fail_state
```

`preflight` clones the target repo to `local_repo_path` if it is missing or not a git repository. If the clone fails the graph routes immediately to `fail_state` before any LLM work begins.

### Workflow Stages

1. **preflight** — Clones the target repository to `local_repo_path` if the directory is missing or not a git repo. Any failure aborts immediately before any LLM work begins.
2. **load_project_context** — Validates the repo, loads skills, and generates a compressed project summary (cached in Redis → PostgreSQL). Fails immediately if `--repo` does not match the remote origin.
3. **read_issue** — Fetches issue title + body from GitHub.
4. **plan** — Single LLM call producing a structured implementation plan; parses the "Ordered steps" section into a `list[str]` of discrete tasks. Resets `step_idx`, `step_results`, and `modified_files` for a clean execution cycle. Result cached (L1 local LRU + L2 Redis, 60 s TTL) keyed by `user_id:repo:issue:retry_count`.
5. **execute_step** — Runs one ReAct loop per plan step. Loops back to itself via `route_after_execute` until all steps are done, then advances to `reflect`. Accumulates `modified_files` (deduplicated) and `step_results` across iterations; the last step's test code is forwarded to the test node.
6. **reflect** — Logs a summary of what was accomplished across all steps. Informational only — no state mutation.
7. **test** — Writes the generated test file to the repo, runs it in-place using the project's detected test runner (`pytest`, `jest`, `go test`, etc.), then removes the file. Returns `success` or `retry`.
8. **tester** — On failure: analyzes execution output and emits a verdict:
   - `APPROVED` — solution satisfies the issue requirements (tests were wrong but code is correct); routes directly to PR creation.
   - `NEEDS_WORK` — structured feedback (root cause, code issues, test issues, suggestions) fed back into the next `plan` call.
9. **create_pr** — Creates a new branch, commits the solution, pushes, and opens a pull request directly.

## Quick Start

### Usage

```bash
python main.py run --repo owner/repo --issue 42 --path /path/to/repo [--base main]
# Or start the HTTP server (default):
python main.py
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
- GitHub credentials (PAT or App)
- Redis (for context caching)
- PostgreSQL with pgvector (optional, for semantic search)
- `ast-grep` CLI (for code analysis)
- Node.js / `npx` (for skills discovery)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/story-pr-agent.git
cd story-pr-agent
```

### 2. Install Python dependencies

```bash
uv sync
```

### 3. Install ast-grep

```bash
npm install -g @ast-grep/cli
```

### 4. Start Redis

```bash
redis-server
# Or via Docker:
docker run -d -p 6379:6379 redis:latest
```

### 5. Configure environment variables

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
python main.py run \
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
| `--user` | no | User ID for cache key (default: `cli`) |

### Via Flask REST API

```bash
python main.py serve --port 8000
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

### Preflight

Before any LLM call or context work, the agent runs a preflight node that clones the target repository to `local_repo_path` if the directory is missing or not a git repository. If the clone fails, the graph routes immediately to `fail_state` and no tokens are spent.

If the repo is already present the node is a no-op and execution continues immediately.

### Repo Validation

Before processing, the agent validates:
- ✓ Local path exists and is a git repository
- ✓ Remote origin is configured and accessible
- ✓ `--repo` matches the remote origin URL

If `--repo` does not match the remote origin, the workflow sets `status: repo_mismatch` and routes directly to `fail_state` — no skills loading, no context generation, no LLM calls. The CLI exits with code 1 and logs the conflicting repo and path.

### Skills Detection

After generating the compressed project context, the agent runs `npx skills find` with the full context text as the search query. Matching skills are then installed automatically via `npx skills add`.

Requirements:
- Node.js must be installed (`npx` available on `$PATH`)
- Internet access to reach the skills.sh registry

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
- **Injected into all prompts** to guide code generation

### Plan-Execute-Reflect Loop

The core of the agent is a three-phase loop replacing the old single-shot code node:

#### 1. Plan

A single LLM call produces a structured implementation plan for the issue. The planner outputs a markdown document with an **Ordered steps** section; the agent parses this into a `list[str]` of discrete tasks (e.g. "Add model field", "Update serializer", "Write migration").

The plan is cached per retry attempt so a downstream failure doesn't force re-planning. On tester retry the plan node re-runs with the tester's structured feedback, producing a revised step list for the next execution cycle.

#### 2. Execute (per-step ReAct loop)

`execute_step` runs one ReAct loop for each plan step, cycling back to itself until all steps are done:

| Step | Tools used |
|------|-----------|
| Explore structure | `list_directory`, `read_file` |
| Locate targets | `find_functions`, `search_code_patterns` |
| Apply changes | `str_replace_in_file` (targeted edit), `write_file` (new file) |
| Finish last step | Emits `### TESTS <unit test code>` |

Each iteration accumulates `modified_files` (deduplicated across steps) and a `step_results` summary. The loop cap is 10 ReAct iterations per step and 5000 output tokens per LLM call.

#### 3. Reflect

After all steps complete, the reflect node logs the full plan and per-step summaries. It makes no state changes — it exists purely for observability before handing off to the test node.

### In-Repo Test Execution

Tests run directly in the local repository clone — no Docker sandbox required:

1. The generated test file is written to the repo root under a runner-specific filename (`test_solution.py`, `test_solution.test.js`, etc.)
2. The test runner command is executed with `cwd=local_repo_path`
3. The test file is removed in a `finally` block regardless of outcome

Supported runners and their commands:

| Runner | Command |
|--------|---------|
| `pytest` | `pytest test_solution.py -v` |
| `jest` | `npx jest test_solution.test.js --no-coverage` |
| `go test` | `go test ./...` |
| `cargo test` | `cargo test` |
| `unittest` | `python -m unittest test_solution` |

### Iterative Coder-Tester Loop

After each full execute cycle:
1. The generated tests run in the local repo using the project's detected test runner.
2. On failure, the **tester** analyzes execution output and emits a verdict:
   - `APPROVED` — solution satisfies the issue requirements; routes directly to PR creation.
   - `NEEDS_WORK` — root cause, code issues, test issues, and concrete suggestions are fed back into the next `plan` call.
3. On retry, `plan` re-runs with the tester's feedback and resets `step_idx`, `step_results`, and `modified_files` for a fresh execution cycle.
4. The loop repeats until tests pass, an `APPROVED` verdict is received, or `MAX_RETRIES` is exhausted.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | **Required** Anthropic API key |
| `GITHUB_TOKEN` | — | GitHub Personal Access Token (auth method A) |
| `GITHUB_APP_ID` | `3770889` | GitHub App ID (auth method B) |
| `GITHUB_PRIVATE_KEY_PATH` | — | Path to GitHub App private key |
| `GITHUB_INSTALLATION_ID` | — | GitHub App installation ID |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for webhook requests |
| `GITHUB_CLIENT` | — | GitHub OAuth App client ID (web UI login) |
| `GITHUB_SECRET` | — | GitHub OAuth App client secret (web UI login) |
| `GOOGLE_OAUTH_CLIENT_ID` | — | Google OAuth client ID (optional, web UI) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | — | Google OAuth client secret (optional, web UI) |
| `MAX_RETRIES` | `3` | Max full plan→execute→test cycles before giving up |
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
├── main.py                         # CLI entry point (run / serve subcommands)
├── app.py                          # Flask REST API server
├── webhook.py                      # FastAPI webhook server (GitHub events)
├── graph.py                        # LangGraph state machine
├── state.py                        # State schema & routing functions
├── config.py                       # Settings, LLMClient, LLM factory helpers
├── .env.example                    # Environment variable template
├── actions/
│   ├── git_nodes.py               # Git operations: read issue, create PR
│   ├── llm_nodes.py               # plan_solution, execute_step, reflect, test_code, tester_review
│   ├── context_nodes.py           # Preflight, repo validation, skills loading, context gen
│   ├── planner_prompt.md          # Structured planning prompt (outputs Ordered steps section)
│   ├── coder_prompt.md            # Per-step ReAct coder prompt (explore → edit → output tests)
│   └── tester_prompt.md           # Failure analysis prompt (APPROVED / NEEDS_WORK)
├── cache/
│   ├── __init__.py                # Public API: get_local_cache(), make_cache_key()
│   ├── _llm.py                    # LLM response cache (L1 local + L2 Redis)
│   ├── _redis.py                  # RedisClient with retry on connection errors
│   └── _pg.py                     # PostgreSQL persistent context cache (L2 fallback)
├── tools/
│   ├── _retry.py                  # Shared RetryableError + tenacity config constants
│   ├── executor.py                # TestRunner: writes test file, runs in-repo, cleans up
│   ├── github.py                  # GitHubClient + GitHubOAuthClient (retryable)
│   ├── git_ops.py                 # Git CLI operations (branch, commit, push)
│   ├── npx_client.py              # NPXClient: skills find/add via npx subprocess
│   ├── ast_grep.py                # ast-grep CLI wrapper
│   ├── langchain_tools.py         # LLM-callable tools: search + file read/write/edit
│   ├── context_generator.py       # Project context generation
│   └── context_compression.py    # Context compression strategies
├── auth/
│   └── db.py                      # MongoDBClient (retryable) + module-level helpers
├── cli_runner/
│   ├── runner.py                  # Parallel check runner + colored result formatter
│   └── checks/                   # Individual service health check modules
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
python main.py run --repo myorg/myproject --issue 123 --path /path/to/myproject

# Agent:
# 1. Plans: ["Add model field", "Update serializer", "Add view", "Write pytest tests"]
# 2. Executes each step via ReAct loop (reads files, edits in-place)
# 3. Runs pytest directly in the repo
# 4. Opens PR with all changes
```

### Example 2: Node.js Express API

```bash
# Repository with skills.sh:
language: javascript
framework: express
test_runner: jest

# Issue: "Add rate limiting middleware"
python main.py run --repo myorg/api --issue 456 --path /path/to/api

# Agent:
# 1. Plans: ["Install express-rate-limit", "Write middleware", "Wire into app", "Write jest tests"]
# 2. Executes each step, accumulating modified files across steps
# 3. Runs jest directly in the repo
# 4. Opens PR ready to merge
```

## Pre-run Connectivity Check

Before running the agent, verify all external service connections:

```bash
python -m cli_runner                          # check all services
python -m cli_runner --only github,ast_grep   # check specific services
python -m cli_runner --list                   # list available checks
python -m cli_runner --verbose                # show full tracebacks on failure
```

Note: the preflight node inside the graph runs the GitHub and ast-grep checks automatically on every run. `cli_runner` is for manual validation and CI gating before invoking the agent.

## Troubleshooting

### Redis Connection Error
- Ensure Redis is running: `redis-cli ping`
- Check `REDIS_URL` in `.env`

### Repo Validation Fails
- **`--repo` mismatch**: The `--repo owner/repo` value must match the remote origin of the local clone at `--path`. Check with `git -C /path/to/repo remote get-url origin`.

### Test Execution Fails
- Ensure the detected test runner is installed in the target repo's environment (e.g. `pytest`, `jest`).
- The test file is always cleaned up — check the repo for a leftover `test_solution.*` file if a crash occurred.

### No Skills Discovered
- Ensure `npx` is available: `npx --version`
- Check internet access — the skills.sh registry must be reachable
- A warning is logged and the agent continues without skills if none are found

### LLM Generation Fails
- Check `ANTHROPIC_API_KEY` is valid
- Verify Claude model in `.env` is available
- Check token limits (Claude Sonnet has 200K context)
- LLM calls time out after 120 s read / 10 s connect and retry up to 4 times on 429/5xx

## Contributing

Contributions welcome! When adding support for new languages/frameworks:

1. Add the test runner command to `_TEST_COMMANDS` in `tools/executor.py`
2. Add language support to `context_generator.py`
3. Update `skills.sh` example in this README
4. Add to "Capabilities by Language" table
5. Test with example repository

## License

MIT

## Support

- 🐛 Issues: GitHub Issues
- 💬 Discussions: GitHub Discussions
- 📖 Docs: See README.md
