# Architecture

This document describes the design and architecture of story-pr-agent.

## Design Philosophy

**story-pr-agent** is built on three core principles:

1. **Generic by Default** — Works with any codebase, any language, any framework
2. **Skills-Aware** — Uses project metadata to customize generation
3. **Context-Informed** — Analyzes the actual codebase to match patterns

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     User Interface Layer                        │
├─────────────────────────────────────────────────────────────────┤
│  CLI (main.py)   Webhook Server (webhook.py)                    │
└────────────────────┬────────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────────┐
│              LangGraph State Machine (graph.py)                 │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Context Nodes (actions/context_nodes.py)                │   │
│  │  - repo_validation: Check local == remote                │   │
│  │  - load_skills: Parse skills.sh                          │   │
│  │  - load_project_context: Analyze codebase                │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Git Nodes (actions/git_nodes.py)                        │   │
│  │  - preflight: GitHub scope + push + draft PR + ast-grep  │   │
│  │  - read_github_issue: Fetch from GitHub API              │   │
│  │  - create_pr: Push real code, promote draft PR           │   │
│  │  - handle_failure: Log and finalize on all failure paths │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  LLM Nodes (actions/llm_nodes.py)                        │   │
│  │  - plan_solution: Single LLM call → implementation plan  │   │
│  │  - code_solution: ReAct loop → in-place file edits       │   │
│  │  - test_code: Execute in Docker sandbox                  │   │
│  │  - tester_review: Failure analysis → APPROVED/NEEDS_WORK │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────────┐
│               Tool Layer (tools/)                               │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │  Storage        │  │  GitHub API  │  │  Git Operations    │  │
│  │  (Redis, PG)    │  │  (PyGithub)  │  │  (subprocess)      │  │
│  └─────────────────┘  └──────────────┘  └────────────────────┘  │
│  ┌─────────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │  Code Search    │  │  Testing     │  │  Context Gen       │  │
│  │  (ast-grep)     │  │  (Docker)    │  │  & Compression     │  │
│  └─────────────────┘  └──────────────┘  └────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  File Tools (ReAct coder)                                │   │
│  │  - read_file, list_directory                             │   │
│  │  - write_file, str_replace_in_file                       │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────-┘
```

## Execution Flow

### 1. Input Validation & Setup

```
User Input
    │
    └─► CLI (main.py)
        ├─ Parse arguments (--repo, --issue, --path, --base)
        ├─ Initialize LangGraph
        ├─ Build AgentState with initial values
        └─ Invoke graph.invoke(state)
```

### 2. State Machine Execution

The graph passes state through nodes in sequence:

```
START
  │
  ├─► preflight
  │   ├─ Check: GitHub token scopes (PAT: repo/public_repo | App: pull_requests+contents write)
  │   ├─ Push: empty commit to fix/issue-{N} → confirms push access
  │   ├─ Create: draft PR on that branch → confirms PR creation access
  │   ├─ Check: ast-grep --version → confirms CLI on PATH
  │   ├─ If any fail:
  │   │   └─ Return: {status: "preflight_failed", preflight_error: "<message>"}
  │   │   └─ [route_after_preflight → fail_state]  ← no tokens spent
  │   └─ Return: {preflight_pr_number: <pr_number>}
  │
  ├─► repo_validation
  │   ├─ Read: repo_name, local_repo_path
  │   ├─ Validate: git remote matches repo_name
  │   ├─ Validate: working directory is clean
  │   └─ Return: {validation_status: "ok" or error}
  │
  ├─► load_skills
  │   ├─ Read: skills.sh from local_repo_path
  │   ├─ Parse: language, framework, test_runner, etc.
  │   └─ Return: {skills: {...}, language: "python", ...}
  │
  ├─► load_project_context
  │   ├─ Check: Redis cache for existing context
  │   ├─ If cached:
  │   │   └─ Return: {project_context: cached_value, ...}
  │   ├─ If not cached:
  │   │   ├─ Generate: File structure, functions, classes
  │   │   ├─ Compress: Apply token reduction strategies
  │   │   ├─ Store: Save to Redis + PostgreSQL
  │   │   └─ Return: {project_context: generated_value, ...}
  │
  ├─► read_issue
  │   ├─ Call: GitHub API to fetch issue
  │   ├─ Read: issue title and body
  │   └─ Return: {issue_details: formatted_string, retry_count: 0, max_retries: N}
  │
  ├─► plan_solution
  │   ├─ Call: Plain LLM (no tools) — project_context already complete
  │   │   ├─ Input: issue_details, project_context
  │   │   └─ Output: Structured plan (approaches, steps, risks)
  │   └─ Return: {implementation_plan: plan_text}
  │
  ├─► code_solution (ITERATIVE — ReAct loop)
  │   ├─ Call: Claude with ReAct loop (max 10 tool iterations, 5000 token cap)
  │   │   ├─ Input: implementation_plan, issue_details, project_context, tester_feedback
  │   │   ├─ Tools: find_functions, search_code_patterns (search)
  │   │   │         read_file, list_directory (explore)
  │   │   │         str_replace_in_file, write_file (edit in-place)
  │   │   └─ Output (final text): "### TESTS\n<tests>" only
  │   ├─ Track: each write_file/str_replace_in_file call → modified_files list
  │   ├─ Read back: modified_files contents → concatenated as code_snippet for executor
  │   └─ Return: {modified_files: [...], code_snippet: concatenated, test_code: unit_tests}
  │
  ├─► test_code
  │   ├─ Call: Docker executor with skills.test_runner dispatch
  │   │   ├─ Mount: solution.py, test_solution.py into container
  │   │   ├─ Run: command from _TEST_COMMANDS[test_runner]
  │   │   └─ Return: success/failure + raw output
  │   ├─ If success:
  │   │   └─ Return: {status: "success", test_results: output}
  │   └─ If failure:
  │       └─ Return: {status: "retry", test_results: error_output}
  │       └─ [route_after_test() → tester_review]
  │
  ├─► tester_review (on test failure)
  │   ├─ Call: Plain LLM (5000 token cap)
  │   │   ├─ Input: implementation_plan, code_snippet, test_code, test_runner, test_results
  │   │   └─ Output: "VERDICT: APPROVED|NEEDS_WORK\n<feedback>"
  │   ├─ Parse: Extract verdict from first line
  │   ├─ Increment: retry_count
  │   ├─ If APPROVED (early termination):
  │   │   └─ Return: {status: "tester_approved", tester_feedback: response}
  │   └─ If NEEDS_WORK:
  │       └─ Return: {tester_feedback: structured_feedback}
  │       └─ [route_after_tester() → code_solution or handle_failure]
  │
  ├─► [route_after_tester() decision]
  │   ├─ If status="tester_approved"  → create_pr (early termination)
  │   ├─ If retry_count < max_retries → code_solution (loop with feedback)
  │   └─ Else                         → handle_failure
  │
  ├─► create_pr (on success or tester APPROVED)
  │   ├─ Stash: agent's in-place edits
  │   ├─ Fetch + reset: sync base_branch from origin
  │   ├─ Checkout: fix/issue-N (already exists from preflight)
  │   ├─ Pop stash: restore agent edits onto feature branch
  │   ├─ Stage: all paths in modified_files
  │   ├─ Commit + Push: real solution onto the preflight branch
  │   ├─ Promote: update_pull_request(preflight_pr_number) → title/body + draft=False
  │   └─ Return: {status: "pr_created", pr_url: ...}
  │
  ├─► handle_failure (on max iterations exceeded)
  │   ├─ Log: retry_count and last tester_feedback
  │   └─ Return: {status: "failed"}
  │
  └─► END
      └─ Return: final_state with pr_url or failure reason
```

## Key Architectural Decisions

### 1. State-Driven, Not Exception-Driven

**Design**: State changes are communicated via returned dicts, not exceptions.

```python
# ✓ What we do
def test_code(state):
    result = execute_python_tests(...)
    if result["success"]:
        return {"status": "success", ...}
    else:
        return {"status": "retry", "retry_count": retry_count + 1, ...}

# ✗ What we DON'T do
def test_code(state):
    result = execute_python_tests(...)
    if not result["success"]:
        raise TestFailedError()  # No - state-driven instead
```

**Why**: Allows sophisticated routing logic. The `route_after_test()` function reads state to decide whether to retry, create PR, or fail. This is cleaner than exception handling.

### 2. Layered Tool Architecture

**Design**: Three-layer tool structure.

```
Layer 1: LangChain Tools (@tool decorators)
├─ Called by Claude via tool use
├─ Accept LLM-friendly parameters
└─ Return human-readable strings

Layer 2: Implementation Functions
├─ Core logic (search_functions, search_patterns)
├─ Handle parsing, formatting
└─ Return structured dicts

Layer 3: External Services
├─ ast-grep CLI
├─ GitHub API
├─ Docker daemon
└─ Redis/PostgreSQL
```

**Why**: Separation of concerns. LLM tools are user-friendly. Implementation functions are testable. External services are isolated.

### 3. Skills-Based Generation

**Design**: `skills.sh` in repository declares project metadata.

```bash
language: "python"
framework: "django"
test_runner: "pytest"
```

**Why**: Enables **generic** code generation. Same agent can generate Python, JavaScript, Go, etc. The skills inform:
- Prompt template selection
- Syntax validation
- Test command execution
- Code pattern matching

### 4. Context Caching Strategy

**Design**: Project context is cached in two layers keyed by `user_id + repo_name`.

```
get(key)
  → Redis L1 (60 s TTL)   hit  → return                    [< 1 ms]
                           miss ↓
  → PostgreSQL L2          hit  → backfill Redis → return   [< 10 ms]
                           miss → compute fresh → write both layers
```

Cache key scheme: `ctx:{user_id}:{owner_repo}` — scoped per user so different
users get independent cache entries for the same repo. The key builder lives in
`_make_context_key()` in `context_nodes.py`; extend it there when finer-grained
invalidation is needed (e.g. append commit SHA for per-commit caching).

**Why**:
- **Performance**: Eliminates repeated LLM summarisation on retries and re-runs
- **Resilience**: Redis eviction or restart degrades to L2, never to a full recompute
- **Failure isolation**: Any cache error is caught and logged; the agent falls through to a fresh compute rather than crashing

### 5. Node Result Caching (plan + code)

**Design**: `plan_solution` and `code_solution` each have their own two-level cache independent of the context cache.

```
L1: LocalCache (in-process LRU OrderedDict, bounded: 100 entries / 100 MB)
         └── backed by a binary map file on disk for warm restarts

L2: Redis (same Redis instance as context cache, no TTL — kept until evicted)
```

Cache keys:
- `plan`: `node:plan:{user_id}:{repo_name}:{issue_number}`
- `code`: `node:code:{user_id}:{repo_name}:{issue_number}:{retry_count}`

On a `plan` cache hit: the LLM call is skipped; the cached `implementation_plan` is returned directly.

On a `code` cache hit: the cached `modified_files`/`code_snippet`/`test_code` is returned **and** each file write is re-applied to the working tree, keeping the repo consistent even if the process recovered from a downstream failure mid-run.

**LocalCache LRU eviction**: the in-process store is an `OrderedDict`. Every `get` moves the entry to the MRU end. Every `set` inserts at MRU, then evicts from the LRU end until both limits are satisfied.

**Why**: Expensive ReAct-loop runs (potentially 10 × max_retries tool calls) should survive transient failures — Docker timeouts, network blips, process restarts — without forcing a full redo. The per-retry-count key ensures that on a real retry (tester feedback path) the coder re-runs fresh rather than replaying the stale result.

### 6. Repository Validation

**Design**: Before any PR operations, validate local repo matches remote.

```python
def repo_validation(state):
    # Check: local path is git repo
    # Check: remote origin configured
    # Check: local branch matches remote
    # Check: no uncommitted changes
    # Fail if any check fails
```

**Why**: Prevents accidental PR creation, ensures data consistency, protects repository integrity.

### 7. Planning Before Coding

**Design**: A dedicated `plan_solution` node runs before `code_solution` using a single plain LLM call (no tools).

```
issue_details + project_context
        ↓
   plan_solution (plain LLM)
        ↓
  implementation_plan (structured text)
  - selected approach
  - affected components
  - ordered steps
  - risks & mitigations
```

**Why**: Separates reasoning from implementation. The planner uses the cached `project_context` as-is — no codebase re-exploration — keeping it cheap and fast. The coder receives a concrete plan rather than reasoning from scratch each iteration.

### 8. ReAct Coder: In-Place File Editing

**Design**: `code_solution` runs `_run_react_loop`, which gives Claude file read/write tools alongside the existing ast-grep search tools. The agent modifies existing repo files directly rather than generating a standalone snippet.

```python
def _run_react_loop(prompt, variables, repo_path):
    tools = get_coder_tools()          # search + read_file + write_file + str_replace_in_file
    modified_files = []
    messages = prompt.format_messages(**variables)
    for _ in range(_MAX_LOOP_ITERATIONS):
        response = llm_with_tools.invoke(messages)
        if not response.tool_calls:
            test_code = response.content.partition("### TESTS")[2].strip()
            return modified_files, test_code
        for call in response.tool_calls:
            result = tools[call["name"]].invoke({**call["args"], "repo_path": repo_path})
            messages.append(ToolMessage(result))
            if call["name"] in {"write_file", "str_replace_in_file"}:
                modified_files.append(call["args"]["path"])
```

The agent's final response contains only `### TESTS`. The modified files are read back and concatenated as `code_snippet` for the sandboxed executor. On retry, prior edits are already in place — the coder applies targeted `str_replace_in_file` fixes rather than rewriting from scratch.

**Why**: Real code modifications match how engineers actually work — reading existing code, making surgical edits — rather than producing a context-free snippet that then has to be spliced in. It also handles multi-file changes naturally via the `modified_files` list.

### 9. Stash-Based Branch Creation

**Design**: Because the ReAct agent writes files directly to `local_repo_path` before `create_pr` runs, a naive `git reset --hard` would destroy those changes. `create_branch_and_commit` uses a stash round-trip:

```
git stash --include-untracked   ← preserve agent edits
git fetch + checkout + reset    ← sync to clean base_branch
git checkout -b fix/issue-N     ← create feature branch
git stash pop                   ← restore agent edits onto branch
git add <modified_files>        ← stage only tracked paths
git commit + push
```

**Why**: Keeps the branch creation deterministic (always starts from a fresh `base_branch`) while preserving in-place edits the ReAct agent made to arbitrary paths in the working tree.

### 10. Preflight: Permission Probe Before Token Spend

**Design**: The first node pushes an empty branch and creates a draft PR to confirm end-to-end GitHub write access, then verifies `ast-grep` is on PATH. All four checks run before any LLM call.

```
preflight_check
  1. check_github_permissions()   ← token scopes / App permissions
  2. push_empty_branch()          ← confirms push access to origin
  3. create_draft_pr()            ← confirms PR creation access; stores pr_number
  4. ast-grep --version           ← confirms code search CLI available
```

On any failure: `status = "preflight_failed"`, `preflight_error = "<message>"` → `fail_state` immediately.

On success: `preflight_pr_number` is stored in state. `create_pr` calls `update_pull_request(pr_number)` to promote the draft (update title/body, set `draft=False`) rather than opening a new PR.

**Why**: Expensive LLM work (context generation, planning, ReAct loop) should never start if the agent can't deliver its output. A permission failure discovered after a 2-minute coder loop is far more costly than a 3-second preflight probe.

**`test_tool/`** — standalone CLI for manual pre-run validation and CI gating. Runs the same GitHub and ast-grep checks (plus Redis, PostgreSQL, MongoDB, Docker) in parallel outside the graph:

```bash
python -m test_tool                        # all checks
python -m test_tool --only github,ast_grep # subset
python -m test_tool --verbose              # show tracebacks
```

Exit code `0` = all configured checks passed; `1` = any failure.

### 11. Tester as Failure Analyst with Early Termination

**Design**: On test failure, `tester_review` runs before any coder retry. It emits a structured verdict on the first line.

```
VERDICT: APPROVED   → solution is correct; tests were wrong → route to create_pr
VERDICT: NEEDS_WORK → coder must fix; feedback follows below
```

`route_after_tester()` reads `state["status"]` for `"tester_approved"` to trigger early termination, bypassing remaining iterations.

**Why**: Raw test output (assertion errors, tracebacks) is noisy. The tester translates it into actionable root-cause analysis targeting the specific issue — solution bugs vs. test bugs. The APPROVED path prevents unnecessary retries when the solution is already correct but the generated tests were flawed.

### 12. Token Caps for Agent Cost Control

**Design**: Two LLM instances in `config.py`.

```python
llm          = ChatAnthropic(max_tokens=4096)   # planner, context nodes
_bounded_llm = ChatAnthropic(max_tokens=5000)   # coder (agentic loop), tester
```

`get_bounded_llm_with_tools()` binds `_bounded_llm` for the coder agentic loop.  
`get_bounded_llm()` returns `_bounded_llm` for the tester.

**Why**: The coder loop runs up to 10 × max_retries times — unbounded output tokens would dominate cost. 5000 tokens is sufficient for a solution + unit tests. The planner uses the default 4096 cap since it is a single one-shot call.

## State Schema

```python
class AgentState(TypedDict):
    # Input
    user_id: str              # authenticated user ID (cache key scope)
    repo_name: str            # "owner/repo"
    issue_number: int         # GitHub issue number
    local_repo_path: str      # /path/to/local/clone
    base_branch: str          # "main", "develop", etc.

    # Context (loaded once, cached L1→L2)
    skills: dict              # {language, framework, test_runner, ...}
    project_context: str      # compressed repo summary (cached)
    issue_details: str        # formatted issue (title + body)

    # Planning
    implementation_plan: str  # structured plan from plan_solution

    # Preflight
    preflight_pr_number: int  # draft PR number created by preflight; promoted by create_pr
    preflight_error: str      # human-readable failure reason (set when preflight fails)

    # Generated (ReAct coder edits files in-place)
    modified_files: list[str] # repo-relative paths written by the ReAct coder
    code_snippet: str         # concatenated content of modified_files (for sandbox executor)
    test_code: str            # unit tests from the coder's final ### TESTS response

    # Execution & feedback loop
    test_results: str         # raw Docker execution output
    tester_feedback: str      # structured analysis from tester_review
    retry_count: int          # incremented by tester_review per full iteration
    max_retries: int          # initialized by read_github_issue

    # Output
    status: str               # "success" | "retry" | "tester_approved" | "failed" | "pr_created"
    pr_url: str               # URL of opened PR
```

### Status Transitions

```
preflight (fail)  → status = "preflight_failed" → route_after_preflight → fail_state (no tokens spent)
preflight (ok)    → (no status set, preflight_pr_number stored)
read_issue        → (no status set)
test_code (pass)  → status = "success"           → route_after_test → create_pr
test_code (fail)  → status = "retry"             → route_after_test → tester_review
tester_review     → status = "tester_approved"   → route_after_tester → create_pr  (early exit)
                   (NEEDS_WORK, no status change) → route_after_tester → code or fail_state
handle_failure    → status = "failed"
create_pr         → status = "pr_created"        (promotes preflight draft PR)
```

## Language Support Strategy

The system supports multiple languages through:

1. **Language Detection** — From `skills.sh` or code analysis
2. **Language-Specific Prompts** — Different instructions per language
3. **Test Framework Mapping** — pytest, jest, unittest, etc.
4. **Syntax Validation** — Check generated code compiles
5. **Pattern Matching** — Use ast-grep for language-appropriate searches

### Adding New Language Support

1. Create language-specific prompt templates
2. Add to `context_generator.py` for analysis
3. Update `skills.sh.example` with new language options
4. Test with example repository
5. Update README "Capabilities" table

## Performance Considerations

### Token Budget

| Node | LLM instance | Max output tokens | Calls per run |
|---|---|---|---|
| `load_project_context` | `llm` (4096) | 4096 | 2 (cache miss) / 0 (hit) |
| `plan_solution` | `llm` (4096) | 4096 | 1 (node cache miss) / 0 (hit) |
| `code_solution` (each ReAct iteration) | `_bounded_llm` (5000) | 5000 | 1–10 per attempt (node cache miss) / 0 (hit) |
| `tester_review` | `_bounded_llm` (5000) | 5000 | 1 per retry |

Worst-case total output tokens: `2×4096 + 1×4096 + (4 attempts × 10 iterations × 5000) + (3 reviews × 5000)` = **~227K output tokens**. The dominant cost driver is the coder agentic loop; reducing `_MAX_LOOP_ITERATIONS` (default 10) is the highest-leverage knob.

### Caching Strategy

**Context cache** (project summary — Redis → PostgreSQL):
- **L1 Redis** — sub-millisecond retrieval, 60 s TTL auto-eviction
- **L2 PostgreSQL** — persistent fallback, backfills L1 on miss
- Cache keyed by `user_id + repo`; extend key scheme in `_make_context_key()` for finer invalidation

**Node result cache** (plan + code — LocalCache → Redis):
- **L1 LocalCache** — in-process LRU `OrderedDict` (100 entries / 100 MB); backed by a binary map file for warm restarts
- **L2 Redis** — shared across processes; no TTL
- Cache keyed by `node:plan:{user_id}:{repo}:{issue}` / `node:code:{user_id}:{repo}:{issue}:{retry}`

### Timeouts
- Test execution: 60 seconds
- Tool execution: 30 seconds per search
- Graph execution: No hard limit (depends on retries)

## Security & Isolation

### Code Execution Isolation
```
Docker Container
├─ No network access (--network none)
├─ Memory limit (128MB)
├─ CPU limit (0.5 cores)
├─ Read-only filesystem (except /code)
└─ Automatic cleanup on exit
```

### Secret Management
```
.env file (never committed)
├─ ANTHROPIC_API_KEY
├─ GITHUB_TOKEN or GITHUB_APP_ID
├─ REDIS_URL
└─ POSTGRES_VEC_URL
```

### Repository Access
```
SSH/HTTPS with authentication
├─ Token injected into push URL
├─ Validated before PR creation
└─ Limited to specified repository
```

## Extensibility Points

Users can extend the system by:

1. **Adding nodes** — Create new actions in `actions/` directory
2. **Adding tools** — Extend `tools/` with new capabilities
3. **Custom skills** — Add language/framework support via `skills.sh`
4. **Custom prompts** — Override LLM prompts in `llm_nodes.py`
5. **Custom storage** — Swap Redis/PostgreSQL with other backends

## Testing Strategy

### Unit Tests
- Tool functions (ast-grep, storage, compression)
- Node functions (validation, caching, routing)
- Utilities (formatting, parsing)

### Integration Tests
- Full graph execution on test repository
- Skills detection and loading
- Cache hit/miss scenarios
- Docker test execution

### End-to-End Tests
- Real GitHub issue → PR creation
- Multiple languages and frameworks
- Error recovery and retry logic
- Repository validation

## Deployment Options

### Local Development
```bash
python main.py --repo owner/repo --issue 42 --path .
```

### Webhook Server (Automated)
```bash
uvicorn webhook:app --host 0.0.0.0 --port 8000
```

### CI/CD Integration
```yaml
# GitHub Actions example
- run: python main.py --repo ${{ github.repository }} --issue ${{ github.event.issue.number }} --path .
```

### Docker Container
```bash
docker run -e ANTHROPIC_API_KEY=... -v $PWD:/workspace story-pr-agent:latest
```

## Future Enhancements

1. **Vector DB semantic search** — Find similar code patterns
2. **Breaking change detection** — Analyze API changes before generation
3. **Coverage tracking** — Optimize test coverage over time
4. **Cost tracking** — Monitor LLM usage and costs
5. **Custom formatters** — Hook into project formatters (prettier, black, etc.)
6. **Interactive mode** — Human-in-the-loop PR generation
7. **Multi-repo executor** — Mount actual repo into Docker sandbox instead of a temp dir copy
