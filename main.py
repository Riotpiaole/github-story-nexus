import argparse
import sys
from graph import build_graph


def main():
    parser = argparse.ArgumentParser(description="Turn a GitHub issue into a Pull Request using Claude.")
    parser.add_argument("--repo", required=True, help="GitHub repo in owner/repo format (e.g. octocat/hello-world)")
    parser.add_argument("--issue", required=True, type=int, help="Issue number to resolve")
    parser.add_argument("--path", required=True, help="Path to your local clone of the repo")
    parser.add_argument("--base", default="main", help="Base branch to open the PR against (default: main)")
    args = parser.parse_args()

    app = build_graph()
    initial_state = {
        "repo_name": args.repo,
        "issue_number": args.issue,
        "local_repo_path": args.path,
        "base_branch": args.base,
        "issue_details": "",
        "code_snippet": "",
        "test_results": "",
        "retry_count": 0,
        "max_retries": 3,
        "status": "",
        "pr_url": "",
    }

    print(f"\nStarting agent for {args.repo} issue #{args.issue}...\n")
    final_state = app.invoke(initial_state)

    print("\n--- DONE ---")
    if final_state["status"] == "pr_created":
        print(f"PR created: {final_state['pr_url']}")
    else:
        print("Agent failed to produce a passing solution after max retries.")
        sys.exit(1)


if __name__ == "__main__":
    main()
