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
│  │  - read_github_issue: Fetch from GitHub API              │   │
│  │  - create_pr: Create branch, commit, push, open PR       │   │
│  │  - handle_failure: Log and finalize on max retries       │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  LLM Nodes (actions/llm_nodes.py)                        │   │
│  │  - code_solution: Claude generates code (with tools)     │   │
│  │  - generate_tests: Claude generates tests                │   │
│  │  - test_code: Execute in Docker sandbox                  │   │
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
  │   └─ Return: {issue_details: formatted_string, ...}
  │
  ├─► code_solution (ITERATIVE)
  │   ├─ Call: Claude with agentic loop
  │   │   ├─ Input: issue_details, skills, project_context
  │   │   ├─ Tools: find_functions, search_code_patterns
  │   │   └─ Output: Generated code
  │   └─ Return: {code_snippet: generated_code}
  │
  ├─► generate_tests
  │   ├─ Call: Claude with agentic loop
  │   │   ├─ Input: issue_details, code_snippet, skills
  │   │   └─ Output: Generated test code
  │   └─ Return: {test_code: generated_tests}
  │
  ├─► test_code
  │   ├─ Call: Docker executor
  │   │   ├─ Mount: code_snippet, test_code into container
  │   │   ├─ Run: test_runner command (pytest, jest, etc.)
  │   │   └─ Return: success/failure + output
  │   ├─ If success:
  │   │   └─ Return: {status: "success", ...}
  │   ├─ If failure AND retries remaining:
  │   │   └─ Return: {status: "retry", test_results: error_output, ...}
  │   └─ [Conditional routing via route_after_test()]
  │
  ├─► [route_after_test() decision]
  │   ├─ If status="success"
  │   │   └─► create_pr
  │   ├─ If status="retry" AND retry_count < max_retries
  │   │   └─► code_solution (loop back)
  │   └─ Else
  │       └─► handle_failure
  │
  ├─► create_pr (on success)
  │   ├─ Create: New branch (fix/issue-N)
  │   ├─ Commit: Code changes
  │   ├─ Push: To origin
  │   ├─ Open: Pull request
  │   └─ Return: {status: "pr_created", pr_url: ...}
  │
  ├─► handle_failure (on max retries exceeded)
  │   ├─ Log: Error details
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

**Design**: Project context (file structure, functions, patterns) is cached per execution.

```
First execution: Generate → Compress → Cache (Redis) → Inject into prompts
Second execution: Load (Redis) → Inject into prompts
(Cache invalidated per execution = always fresh)
```

**Why**: 
- **Performance**: Avoid re-analyzing project on retries
- **Token efficiency**: Compressed context fits in prompt
- **Flexibility**: Vector DB (PostgreSQL) allows future semantic search

### 5. Repository Validation

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

### 6. Agentic Loop for Code Generation

**Design**: Claude can call tools (code search) iteratively.

```python
def _run_agentic_loop(prompt, input_vars, repo_path):
    while True:
        response = llm_with_tools.invoke(...)
        if response.tool_calls:
            for tool_call in response.tool_calls:
                result = execute_tool(tool_call)
                messages.append(ToolMessage(result))
        else:
            return response.content  # Final code
```

**Why**: Claude can search for existing functions, classes, patterns before generating. Improves quality and consistency.

### 7. Error Recovery via Iteration

**Design**: Test failures feed back to Claude for iteration.

```
Claude generates code → Tests fail
    ↓
Extract error output
    ↓
Pass to Claude: "Previous attempt failed with: <error>"
    ↓
Claude generates improved code
    ↓
Retry (max 3 times)
```

**Why**: Handles common issues (import errors, syntax errors, logic errors) automatically. Most issues are fixable with feedback.

## State Schema

```python
class AgentState(TypedDict):
    # Input
    repo_name: str                           # "owner/repo"
    issue_number: int                        # GitHub issue number
    local_repo_path: str                     # /path/to/local/clone
    base_branch: str                         # "main", "develop", etc.
    
    # Context
    issue_details: str                       # Formatted issue (title + body)
    skills: dict                             # {language, framework, ...}
    project_context: str                     # Compressed project summary
    
    # Generated
    code_snippet: str                        # Generated Python code
    test_code: str                           # Generated pytest code
    
    # Execution
    test_results: str                        # Test output (pass/fail details)
    retry_count: int                         # Current retry attempt
    max_retries: int                         # Max allowed retries
    
    # Output
    status: str                              # "success", "retry", "failed"
    pr_url: str                              # URL of created PR
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
- **Context window**: 200K tokens (Claude Sonnet)
- **System prompt**: ~200 tokens
- **Project context**: ~500 tokens (compressed)
- **Issue + code**: ~1000 tokens
- **Remaining**: ~198K tokens for flexibility

### Caching Strategy
- Redis for fast retrieval (<100ms)
- PostgreSQL vector DB for future semantic search
- Context regenerated per execution (stays fresh)

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
2. **Multi-file generation** — Handle issues requiring multiple files
3. **Breaking change detection** — Analyze API changes before generation
4. **Coverage tracking** — Optimize test coverage over time
5. **Cost tracking** — Monitor LLM usage and costs
6. **Custom formatters** — Hook into project formatters (prettier, black, etc.)
7. **Interactive mode** — Human-in-the-loop PR generation
