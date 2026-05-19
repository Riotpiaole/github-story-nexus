# story-pr-agent

A **generic issue-to-PR generator** that automatically reads GitHub issues, generates solutions tailored to the project's language and architecture, tests them, and opens pull requests. Works with any repository by detecting project skills and patterns.

## Overview

This tool transforms GitHub issues into complete pull requests through an intelligent, project-aware workflow:

```
load_project_context ──(repo mismatch)──────────────────────────────────────────────────────► fail_state
        │
        ▼
   read_issue → code → generate_tests → test ──(pass)──► create_pr
                  ▲                      │
                  └──(fail, retry)───────┘
                                         │
                                  (max retries)
                                         ▼
                                     fail_state
```

`load_project_context` bundles repo validation, skills loading, and context generation. If the `--repo` flag does not match the remote origin, the workflow routes immediately to `fail_state` without proceeding further.

### Workflow Stages

1. **load_project_context** — Validates the repo, loads skills, and generates project understanding. Fails immediately with exit code 1 if `--repo` does not match the remote origin.
2. **read_issue** — Fetches issue title + body from GitHub
3. **code** — Claude generates solution using skills + project context
4. **generate_tests** — Claude writes tests matching project conventions
5. **test** — Runs code + tests in isolated Docker sandbox
6. **create_pr** — Creates branch, commits, pushes, opens PR

## Quick Start

### Usage

```bash
python main.py --repo owner/repo --issue 42 --path /path/to/repo [--base main]
```

### Key Feature: Skills-Based Generation

The agent detects your project type and generates language-appropriate code:

```bash
# skills.sh in your repository root
language: "python"
framework: "django"
test_runner: "pytest"
python_version: "3.11"
```

This enables:
- Language-specific code generation (Python, JavaScript, Go, Rust, Java, etc.)
- Framework-aware patterns (Django ORM, FastAPI, Express, etc.)
- Correct test syntax (pytest, jest, unittest, mocha, etc.)
- Proper package management (pip, npm, cargo, etc.)

## Requirements

- Python 3.12+
- Docker (for sandboxed testing)
- `skills.sh` in repository root (for project detection)
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

The agent reads `skills.sh` to understand:
- What language the project is written in
- What framework/libraries are used
- What test framework is expected
- Project-specific conventions

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

### Code Generation with Skills

Claude receives:
- GitHub issue details
- Detected project skills (language, framework, test runner)
- Project context and patterns
- Available code search tools (ast-grep)

It generates code that:
- Matches the detected language and framework
- Follows project conventions and patterns
- Is structured for the test framework
- Uses appropriate idioms and best practices

### Iterative Testing

Generated code is:
1. Tested immediately in isolated Docker sandbox
2. If tests fail, failure output is fed back to Claude
3. Claude iterates on the solution (max retries)
4. When tests pass, PR is automatically created

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | **Required** Anthropic API key |
| `GITHUB_TOKEN` | — | GitHub Personal Access Token (auth method A) |
| `GITHUB_APP_ID` | — | GitHub App ID (auth method B) |
| `GITHUB_PRIVATE_KEY_PATH` | — | Path to GitHub App private key |
| `GITHUB_INSTALLATION_ID` | — | GitHub App installation ID |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for webhook requests |
| `MAX_RETRIES` | `3` | Max code generation attempts before giving up |
| `LLM_MODEL` | `claude-sonnet-4-6` | Claude model to use for generation |
| `BASE_BRANCH` | `main` | Default PR target branch |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `POSTGRES_VEC_URL` | — | PostgreSQL vector DB (optional, for semantic search) |
| `LANGCHAIN_TRACING_V2` | `false` | Enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | — | LangSmith API key |
| `LANGCHAIN_PROJECT` | `story-pr-agent` | LangSmith project name |

## Project Structure

```
.
├── main.py                         # CLI entry point
├── webhook.py                      # FastAPI webhook server
├── graph.py                        # LangGraph state machine
├── state.py                        # State schema & routing
├── config.py                       # Settings & initialization
├── Dockerfile.runner               # Test execution Docker image
├── requirements.txt                # Python dependencies
├── .env.example                    # Environment variable template
├── actions/
│   ├── git_nodes.py               # Git operations (read issue, create PR)
│   ├── llm_nodes.py               # LLM code generation with tools
│   └── context_nodes.py           # Repo validation, skills loading, context gen
├── tools/
│   ├── executor.py                # Docker test execution
│   ├── github.py                  # GitHub API integration
│   ├── git_ops.py                 # Git CLI operations
│   ├── ast_grep.py                # ast-grep CLI wrapper
│   ├── langchain_tools.py         # LLM-callable tools
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

### skills.sh Not Found
- Agent will proceed but with limited language detection
- Add `skills.sh` to repository root for better results

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
