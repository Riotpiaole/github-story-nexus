import argparse
import logging
import sys

from config import configure_logging, settings
from graph import build_graph

configure_logging()
log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Turn a GitHub issue into a Pull Request using Claude."
    )
    parser.add_argument("--repo", required=True, help="owner/repo (e.g. octocat/hello-world)")
    parser.add_argument("--issue", required=True, type=int, help="Issue number to resolve")
    parser.add_argument("--path", required=True, help="Path to your local clone of the repo")
    parser.add_argument("--base", default=settings.base_branch, help="Base branch (default from config)")
    args = parser.parse_args()

    app = build_graph()
    initial_state = {
        "repo_name": args.repo,
        "issue_number": args.issue,
        "local_repo_path": args.path,
        "base_branch": args.base,
        "issue_details": "",
        "code_snippet": "",
        "test_code": "",
        "test_results": "",
        "retry_count": 0,
        "max_retries": settings.max_retries,
        "status": "",
        "pr_url": "",
    }

    log.info("Starting agent for %s issue #%d...", args.repo, args.issue)
    final_state = app.invoke(initial_state)

    if final_state["status"] == "pr_created":
        log.info("Done. PR: %s", final_state["pr_url"])
        print(final_state["pr_url"])
    else:
        log.error("Agent failed to produce a passing solution.")
        sys.exit(1)


if __name__ == "__main__":
    main()
