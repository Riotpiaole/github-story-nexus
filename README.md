# story-pr-agent

A LangGraph state machine that reads a GitHub issue, uses Claude to generate and iteratively test a solution, then opens a Pull Request automatically.

## How it works

```
read_issue → code → generate_tests → test ──(pass)──► create_pr
                        ▲                  │
                        └──(fail, retry)───┘
                                           │
                                    (max retries)
                                           ▼
                                       fail_state
```

1. **read_issue** — fetches the issue title + body from GitHub
2. **code** — Claude generates a Python solution based on the issue (and any prior test failures)
3. **generate_tests** — Claude writes a pytest file for the generated solution
4. **test** — runs both files inside an isolated Docker container
5. **create_pr** — creates a branch, commits the code, pushes, and opens a PR

## Prerequisites

- Python 3.12+
- Docker (for the sandboxed test runner)
- A GitHub account with either a Personal Access Token or a GitHub App

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Build the Docker test runner image

This only needs to be done once:

```bash
docker build -t story-pr-runner -f Dockerfile.runner .
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Then edit `.env`:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...

# GitHub auth — choose one option:

# Option A: Personal Access Token (easiest for local dev)
GITHUB_TOKEN=ghp_...

# Option B: GitHub App (recommended for production)
# GITHUB_APP_ID=123456
# GITHUB_PRIVATE_KEY_PATH=/path/to/private-key.pem
# GITHUB_INSTALLATION_ID=78901234
```

The token needs these GitHub scopes: `repo`, `pull_requests`.

## Running

### CLI

Point it at any GitHub repo + issue number you have write access to:

```bash
python main.py \
  --repo owner/repo \
  --issue 42 \
  --path /path/to/local/clone
```

Options:

| Flag | Required | Description |
|------|----------|-------------|
| `--repo` | yes | `owner/repo` format |
| `--issue` | yes | Issue number |
| `--path` | yes | Path to your local clone of the repo |
| `--base` | no | Base branch to PR into (default: `main`) |

On success the PR URL is printed to stdout.

### Webhook server

The agent can also be triggered automatically when a GitHub issue is labeled `generate-pr`.

```bash
uvicorn webhook:app --host 0.0.0.0 --port 8000
```

Then configure a GitHub webhook on your repo:

- **Payload URL**: `https://your-server/webhook`
- **Content type**: `application/json`
- **Events**: Issues
- **Secret**: set `GITHUB_WEBHOOK_SECRET` in `.env` to the same value

## Optional: LangSmith tracing

Add these to `.env` to get full traces of every LLM call and graph step:

```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=story-pr-agent
```

## Configuration reference

All settings live in `.env` (see `.env.example` for the full list):

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required |
| `GITHUB_TOKEN` | — | PAT auth |
| `GITHUB_APP_ID` | — | App auth |
| `GITHUB_PRIVATE_KEY_PATH` | — | Path to App `.pem` file |
| `GITHUB_INSTALLATION_ID` | — | App installation ID |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for webhook verification |
| `MAX_RETRIES` | `3` | Max code/test iterations before giving up |
| `LLM_MODEL` | `claude-sonnet-4-6` | Claude model to use |
| `BASE_BRANCH` | `main` | Default PR target branch |

## Project structure

```
.
├── main.py               # CLI entry point
├── webhook.py            # FastAPI webhook server
├── graph.py              # LangGraph state machine definition
├── state.py              # AgentState schema + routing logic
├── config.py             # Settings, LLM client, logging setup
├── Dockerfile.runner     # Isolated test runner image
├── actions/
│   ├── git_nodes.py      # read_issue, create_pr, handle_failure nodes
│   └── llm_nodes.py      # code_solution, generate_tests, test_code nodes
└── tools/
    ├── executor.py       # Runs code + tests inside Docker
    ├── github.py         # GitHub API calls (fetch issue, open PR)
    └── git_ops.py        # Git branch, commit, push operations
```
