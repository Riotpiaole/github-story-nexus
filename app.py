"""
Flask app — exposes a REST API to trigger the LangGraph PR agent.

Endpoints:
  POST /run           start a new agent run (returns run_id immediately)
  GET  /run/<run_id>  poll run status and result
  GET  /health        liveness check

Run with:
    flask --app app run --host 0.0.0.0 --port 8000
"""

import atexit
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import Flask, jsonify, request
from flask_login import current_user, login_required

from auth import init_auth
from config import configure_logging, get_settings
from graph import build_graph

configure_logging()
log = logging.getLogger(__name__)

app = Flask(__name__)
init_auth(app)
_graph = build_graph()

# Bound concurrency to the number of logical CPUs so we never spawn more
# simultaneous graph runs than the machine can actually execute in parallel.
_pool = ThreadPoolExecutor(max_workers=os.cpu_count())
atexit.register(_pool.shutdown, wait=True)

# In-memory run store: {run_id: {"status": ..., ...}}
_runs: dict[str, dict[str, Any]] = {}
_runs_lock = threading.Lock()


def _execute(run_id: str, initial_state: dict) -> None:
    """Run the LangGraph workflow inside a pool worker.

    Invokes the compiled graph with the given initial state and writes the
    outcome back to `_runs` under `run_id`. On success the entry contains
    `agent_status` and `pr_url`; on unhandled exception it contains `error`.

    Args:
        run_id: UUID string used to look up this run via GET /run/<run_id>.
        initial_state: Fully populated AgentState dict passed to graph.invoke().
    """
    try:
        final = _graph.invoke(initial_state)
        agent_status = final.get("status", "unknown")
        with _runs_lock:
            _runs[run_id] = {
                "status": "done",
                "agent_status": agent_status,
                "pr_url": final.get("pr_url", ""),
            }
        log.info("Run %s finished with agent_status '%s'.", run_id, agent_status)
    except Exception as exc:
        log.exception("Run %s raised an unhandled error.", run_id)
        with _runs_lock:
            _runs[run_id] = {"status": "error", "error": str(exc)}


@app.post("/run")
@login_required
def start_run():
    """Start a new agent run.

    Validates the request body, registers the run as 'running', then submits
    the graph to the thread pool and returns immediately so the caller can poll
    GET /run/<run_id> for the result.

    If all pool workers are busy the task is queued; the `run_id` is still
    returned immediately and status remains 'running' until a worker picks it up.

    Request body (JSON):
        repo  (str, required)  — GitHub repository in "owner/repo" format.
        issue (int, required)  — Issue number to resolve.
        path  (str, required)  — Absolute path to the local clone of the repo.
        base  (str, optional)  — Base branch for the PR (default from config).

    Returns:
        202  {"run_id": "<uuid>", "status": "running"}
        400  {"error": "<message>"}  on missing or invalid fields.
    """
    body = request.get_json(silent=True) or {}

    repo = body.get("repo", "").strip()
    issue = body.get("issue")
    path = body.get("path", "").strip()

    if not repo or not path:
        return jsonify({"error": "'repo' and 'path' are required."}), 400
    if not isinstance(issue, int):
        return jsonify({"error": "'issue' must be an integer."}), 400

    s = get_settings()
    run_id = str(uuid.uuid4())
    initial_state = {
        "user_id": current_user.get_id(),
        "repo_name": repo,
        "issue_number": issue,
        "local_repo_path": path,
        "base_branch": body.get("base", s.base_branch),
        "skills": {},
        "project_context": "",
        "issue_details": "",
        "code_snippet": "",
        "test_code": "",
        "test_results": "",
        "retry_count": 0,
        "max_retries": s.max_retries,
        "status": "",
        "pr_url": "",
    }

    with _runs_lock:
        _runs[run_id] = {"status": "running"}

    _pool.submit(_execute, run_id, initial_state)

    log.info("Run %s queued for %s issue #%d (pool workers: %d).", run_id, repo, issue, os.cpu_count())
    return jsonify({"run_id": run_id, "status": "running"}), 202


@app.get("/run/<run_id>")
@login_required
def get_run(run_id: str):
    """Poll the status of an agent run.

    Args:
        run_id: The UUID returned by POST /run.

    Returns:
        200  {"run_id": "...", "status": "running"}                      — still in progress.
        200  {"run_id": "...", "status": "done", "agent_status": "...",
               "pr_url": "..."}                                           — completed.
        200  {"run_id": "...", "status": "error", "error": "..."}        — failed.
        404  {"error": "Run not found."}                                 — unknown run_id.
    """
    with _runs_lock:
        run = _runs.get(run_id)
    if run is None:
        return jsonify({"error": "Run not found."}), 404
    return jsonify({"run_id": run_id, **run})


@app.get("/health")
def health():
    """Liveness check. Returns 200 {"status": "ok"} when the server is up."""
    return jsonify({"status": "ok"})
