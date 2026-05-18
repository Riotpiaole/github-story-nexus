"""
FastAPI webhook server — listens for GitHub issue events and triggers the agent
when the label 'generate-pr' is added to an issue.

Run with:
    uvicorn webhook:app --host 0.0.0.0 --port 8000
"""

import asyncio
import hashlib
import hmac
import logging

from fastapi import FastAPI, Header, HTTPException, Request

from config import configure_logging, get_settings
from graph import build_graph

configure_logging()
log = logging.getLogger(__name__)

app = FastAPI(title="story-pr-agent webhook")
_graph = build_graph()


def _verify_signature(payload: bytes, signature: str | None, secret: str) -> None:
    """Reject requests whose HMAC-SHA256 signature does not match."""
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header.")
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")


async def _run_agent(repo_name: str, issue_number: int) -> None:
    s = get_settings()
    initial_state = {
        "repo_name": repo_name,
        "issue_number": issue_number,
        "local_repo_path": "",     # not used in webhook mode — code is committed via API
        "base_branch": s.base_branch,
        "issue_details": "",
        "code_snippet": "",
        "test_code": "",
        "test_results": "",
        "retry_count": 0,
        "max_retries": s.max_retries,
        "status": "",
        "pr_url": "",
    }
    try:
        final = await asyncio.to_thread(_graph.invoke, initial_state)
        if final["status"] == "pr_created":
            log.info("PR created for %s#%d: %s", repo_name, issue_number, final["pr_url"])
        else:
            log.error("Agent failed for %s#%d after max retries.", repo_name, issue_number)
    except Exception:
        log.exception("Unhandled error running agent for %s#%d.", repo_name, issue_number)


@app.post("/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    s = get_settings()
    payload_bytes = await request.body()

    if s.github_webhook_secret:
        _verify_signature(payload_bytes, x_hub_signature_256, s.github_webhook_secret)

    if x_github_event != "issues":
        return {"status": "ignored", "reason": f"event '{x_github_event}' not handled"}

    payload = await request.json()
    action = payload.get("action")
    label = payload.get("label", {}).get("name", "")

    if action != "labeled" or label != "generate-pr":
        return {"status": "ignored", "reason": "label is not 'generate-pr'"}

    repo_name = payload["repository"]["full_name"]
    issue_number = payload["issue"]["number"]
    log.info("Received 'generate-pr' label on %s#%d — starting agent.", repo_name, issue_number)

    asyncio.create_task(_run_agent(repo_name, issue_number))
    return {"status": "accepted", "issue": issue_number, "repo": repo_name}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
