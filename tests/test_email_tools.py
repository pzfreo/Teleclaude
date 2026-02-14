"""Tests for email_tools.py — Gmail send client and tool dispatch."""

import json
from unittest.mock import MagicMock, patch


class TestGmailSendClient:
    def _make_client(self):
        with (
            patch("email_tools.Credentials") as mock_creds_cls,
            patch("email_tools.Request"),
            patch("email_tools.build") as mock_build,
        ):
            mock_creds = MagicMock()
            mock_creds_cls.return_value = mock_creds
            mock_service = MagicMock()
            mock_build.return_value = mock_service

            from email_tools import GmailSendClient

            client = GmailSendClient("cid", "csec", "rtok")
            return client, mock_service

    def test_send_email(self):
        client, svc = self._make_client()
        svc.users().messages().send().execute.return_value = {"id": "msg1"}
        result = client.send_email("test@example.com", "Subject", "Body text")
        assert result["id"] == "msg1"
        assert result["status"] == "sent"
        assert result["to"] == "test@example.com"

    def test_send_email_with_cc_bcc(self):
        client, svc = self._make_client()
        svc.users().messages().send().execute.return_value = {"id": "msg2"}
        result = client.send_email(
            "to@example.com", "Subject", "Body", cc="cc@example.com", bcc="bcc@example.com"
        )
        assert result["id"] == "msg2"


class TestExecuteTool:
    def test_send_email(self):
        from email_tools import execute_tool

        client = MagicMock()
        client.send_email.return_value = {
            "id": "msg1",
            "status": "sent",
            "to": "test@example.com",
            "subject": "Hello",
        }
        result = execute_tool(client, "send_email", {
            "to": "test@example.com",
            "subject": "Hello",
            "body": "Hi there",
        })
        parsed = json.loads(result)
        assert parsed["status"] == "sent"

    def test_unknown_tool(self):
        from email_tools import execute_tool

        client = MagicMock()
        result = execute_tool(client, "read_email", {})
        assert "Unknown tool" in result

    def test_error_handling(self):
        from email_tools import execute_tool

        client = MagicMock()
        client.send_email.side_effect = RuntimeError("SMTP error")
        result = execute_tool(client, "send_email", {
            "to": "x@y.com",
            "subject": "Test",
            "body": "Body",
        })
        assert "Gmail error" in result
