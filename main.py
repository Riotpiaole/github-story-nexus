import argparse
import logging
import sys

from config import configure_logging, get_langfuse_handler, settings

configure_logging()
log = logging.getLogger(__name__)


def _serve(args: argparse.Namespace) -> None:
    from app import app
    log.info("Starting HTTP server on %s:%d", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=args.debug)


def _run(args: argparse.Namespace) -> None:
    from cache._checkpointer import get_checkpointer
    from graph import build_graph
    checkpointer = get_checkpointer(settings.postgres_checkpoint_url)
    graph = build_graph(checkpointer=checkpointer)
    initial_state = {
        "user_id": args.user,
        "repo_name": args.repo,
        "issue_number": args.issue,
        "local_repo_path": args.path,
        "base_branch": args.base,
        "skills": {},
        "project_context": "",
        "issue_details": "",
        "plan": [],
        "step_idx": 0,
        "step_results": [],
        "modified_files": [],
        "code_snippet": "",
        "test_code": "",
        "test_results": "",
        "retry_count": 0,
        "max_retries": settings.max_retries,
        "status": "",
        "pr_url": "",
    }

    thread_id = f"{args.user}:{args.repo}:{args.issue}"
    config = {"configurable": {"thread_id": thread_id}}
    handler = get_langfuse_handler(thread_id)
    if handler:
        config["callbacks"] = [handler]

    existing = checkpointer.get_tuple(config)
    if existing:
        last_node = existing.metadata.get("source", "unknown")
        log.info("Checkpoint found (last node: %s) — resuming run for %s issue #%d.", last_node, args.repo, args.issue)
    else:
        log.info("No checkpoint found — starting fresh run for %s issue #%d.", args.repo, args.issue)

    final = graph.invoke(initial_state, config=config)

    if final["status"] == "pr_created":
        log.info("Done. PR: %s", final["pr_url"])
        print(final["pr_url"])
    elif final["status"] == "repo_mismatch":
        log.error(
            "--repo '%s' does not match the remote origin of '%s'. Aborting.",
            args.repo, args.path,
        )
        sys.exit(1)
    else:
        log.error("Agent failed to produce a passing solution.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="story-pr-agent: turn GitHub issues into pull requests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py                                      # start HTTP server (default)\n"
            "  python main.py serve --port 9000\n"
            "  python main.py run --repo owner/repo --issue 42 --path /path/to/clone"
        ),
    )

    sub = parser.add_subparsers(dest="command")

    # ── serve (default) ──────────────────────────────────────────────────────
    serve_p = sub.add_parser("serve", help="Start the Flask HTTP server")
    serve_p.add_argument("--host", default="0.0.0.0")
    serve_p.add_argument("--port", default=8000, type=int)
    serve_p.add_argument("--debug", action="store_true")

    # ── run (CLI) ─────────────────────────────────────────────────────────────
    run_p = sub.add_parser("run", help="Run the agent directly from the CLI")
    run_p.add_argument("--repo", required=True, help="owner/repo")
    run_p.add_argument("--issue", required=True, type=int, help="Issue number")
    run_p.add_argument("--path", required=True, help="Path to local clone of the repo")
    run_p.add_argument("--base", default=settings.base_branch, help="Base branch")
    run_p.add_argument("--user", default="cli", help="User ID for cache key (default: cli)")

    args = parser.parse_args()

    if args.command == "run":
        _run(args)
    else:
        # Default to serve when no subcommand is given
        if args.command is None:
            args.host = "0.0.0.0"
            args.port = 8000
            args.debug = False
        _serve(args)


if __name__ == "__main__":
    main()
