"""Gmail send-only tools. No read access — only compose and send."""

import base64
import json
import logging
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


class GmailSendClient:
    """Gmail API client — send only. Uses the gmail.send scope (no read access)."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        creds.refresh(Request(timeout=30))
        self.service = build("gmail", "v1", credentials=creds)

    def send_email(self, to: str, subject: str, body: str, cc: str = "", bcc: str = "") -> dict:
        """Send a plain-text email."""
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        if cc:
            message["cc"] = cc
        if bcc:
            message["bcc"] = bcc

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = self.service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"id": result["id"], "status": "sent", "to": to, "subject": subject}


EMAIL_TOOLS = [
    {
        "name": "send_email",
        "description": "Send an email via Gmail. Can only send — cannot read or search emails. Always confirm with the user before sending.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"},
                "cc": {"type": "string", "description": "CC recipients (comma-separated)", "default": ""},
                "bcc": {"type": "string", "description": "BCC recipients (comma-separated)", "default": ""},
            },
            "required": ["to", "subject", "body"],
        },
    },
]


def execute_tool(client: GmailSendClient, tool_name: str, tool_input: dict) -> str:
    """Execute a Gmail tool call."""
    try:
        if tool_name == "send_email":
            result = client.send_email(
                tool_input["to"],
                tool_input["subject"],
                tool_input["body"],
                tool_input.get("cc", ""),
                tool_input.get("bcc", ""),
            )
            return json.dumps(result, indent=2)
        return f"Unknown tool: {tool_name}"
    except Exception as e:
        return f"Gmail error: {e}"
