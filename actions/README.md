# actions/

LangGraph node implementations called by the state machine in `graph.py`.

## Running the context_nodes integration test

[context_nodes.test.py](context_nodes.test.py) calls `load_project_context` against a real local repo clone with no mocks — repo validation, skills loading, and LLM summarisation all run for real.

### Prerequisites

| Requirement | How to satisfy |
|---|---|
| `ANTHROPIC_API_KEY` | Set in the root `.env` file (the test loads it automatically) |
| Local clone | `/Users/rockliang/workplace/simple_calculator` must exist and be a git repo |
| Remote origin | The clone's `origin` must point to `https://github.com/Riotpiaole/story-nexus-test` |
| `ast-grep` CLI | `npm install -g @ast-grep/cli` |

Verify the remote before running:

```bash
git -C /Users/rockliang/workplace/simple_calculator remote get-url origin
# expected: https://github.com/Riotpiaole/story-nexus-test (or the SSH equivalent)
```

### Run with pytest (recommended)

From the **repo root**:

```bash
uv run pytest actions/context_nodes.test.py -v -s
```

`-s` keeps stdout visible so you can see the printed skills and project context summary.

### Run as a plain script

```bash
uv run python actions/context_nodes.test.py
```

### What the test checks

1. `status` is not `repo_mismatch` — the provided `--repo` matches the remote origin.
2. `project_context` is present and non-trivial (> 100 chars).
3. Prints the parsed `skills` key/value pairs and the first 500 chars of the generated context.

### Changing the target repo

Edit the two constants at the top of `context_nodes.test.py`:

```python
_REPO_NAME = "Riotpiaole/story-nexus-test"
_LOCAL_PATH = "/Users/rockliang/workplace/simple_calculator"
```
