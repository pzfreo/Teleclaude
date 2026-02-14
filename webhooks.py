"""GitHub webhook receiver — notifies Telegram on push, PR, and issue events.

Start with `start_webhook_server(bot, port)` to run alongside the Telegram bot.
Configure with GITHUB_WEBHOOK_SECRET env var for HMAC-SHA256 verification.
"""

import hashlib
import hmac
import json
import logging
import os

from aiohttp import web

logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not secret:
        return True  # skip verification if no secret configured
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _format_push_event(data: dict) -> str | None:
    """Format a push event into a Telegram notification."""
    ref = data.get("ref", "")
    branch = ref.replace("refs/heads/", "")
    repo = data.get("repository", {}).get("full_name", "unknown")
    commits = data.get("commits", [])
    pusher = data.get("pusher", {}).get("name", "unknown")

    if not commits:
        return None

    lines = [f"Push to {repo}/{branch} by {pusher}"]
    for c in commits[:5]:
        msg = c.get("message", "").split("\n")[0][:80]
        sha = c.get("id", "")[:7]
        lines.append(f"  {sha} {msg}")
    if len(commits) > 5:
        lines.append(f"  ... and {len(commits) - 5} more")
    return "\n".join(lines)


def _format_pr_event(data: dict) -> str | None:
    """Format a pull_request event into a Telegram notification."""
    action = data.get("action", "")
    if action not in ("opened", "closed", "merged", "reopened"):
        return None

    pr = data.get("pull_request", {})
    repo = data.get("repository", {}).get("full_name", "unknown")
    title = pr.get("title", "")
    number = pr.get("number", "?")
    user = pr.get("user", {}).get("login", "unknown")
    url = pr.get("html_url", "")

    if action == "closed" and pr.get("merged"):
        action = "merged"

    return f"PR #{number} {action}: {title}\nby {user} on {repo}\n{url}"


def _format_issue_event(data: dict) -> str | None:
    """Format an issues event into a Telegram notification."""
    action = data.get("action", "")
    if action not in ("opened", "closed", "reopened"):
        return None

    issue = data.get("issue", {})
    repo = data.get("repository", {}).get("full_name", "unknown")
    title = issue.get("title", "")
    number = issue.get("number", "?")
    user = issue.get("user", {}).get("login", "unknown")
    url = issue.get("html_url", "")

    return f"Issue #{number} {action}: {title}\nby {user} on {repo}\n{url}"


def _format_event(event_type: str, data: dict) -> str | None:
    """Route an event to the correct formatter."""
    if event_type == "push":
        return _format_push_event(data)
    elif event_type == "pull_request":
        return _format_pr_event(data)
    elif event_type == "issues":
        return _format_issue_event(data)
    return None


def create_webhook_app(bot, notify_chat_ids: set[int]) -> web.Application:
    """Create an aiohttp web app for the webhook endpoint.

    Args:
        bot: Telegram Bot instance for sending notifications
        notify_chat_ids: set of chat IDs to notify on events
    """

    async def handle_webhook(request: web.Request) -> web.Response:
        payload = await request.read()

        # Verify signature
        signature = request.headers.get("X-Hub-Signature-256", "")
        if WEBHOOK_SECRET and not _verify_signature(payload, signature, WEBHOOK_SECRET):
            logger.warning("Webhook signature verification failed")
            return web.Response(status=403, text="Invalid signature")

        event_type = request.headers.get("X-GitHub-Event", "")
        if event_type == "ping":
            return web.Response(text="pong")

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        message = _format_event(event_type, data)
        if message:
            for chat_id in notify_chat_ids:
                try:
                    await bot.send_message(chat_id=chat_id, text=message)
                except Exception as e:
                    logger.warning("Failed to notify chat %d: %s", chat_id, e)

        return web.Response(text="ok")

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/webhook/github", handle_webhook)
    app.router.add_get("/health", health)
    return app


async def start_webhook_server(bot, notify_chat_ids: set[int], port: int = 8080) -> web.AppRunner:
    """Start the webhook HTTP server.

    Returns the runner so it can be cleaned up later.
    """
    app = create_webhook_app(bot, notify_chat_ids)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Webhook server started on port %d", port)
    return runner
