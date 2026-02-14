"""Tests for webhooks.py — GitHub webhook formatting and signature verification."""

import hashlib
import hmac
import json


class TestVerifySignature:
    def test_valid_signature(self):
        from webhooks import _verify_signature

        secret = "test-secret"
        payload = b'{"action":"opened"}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert _verify_signature(payload, sig, secret) is True

    def test_invalid_signature(self):
        from webhooks import _verify_signature

        assert _verify_signature(b"payload", "sha256=wrong", "secret") is False

    def test_missing_prefix(self):
        from webhooks import _verify_signature

        assert _verify_signature(b"payload", "md5=abc", "secret") is False

    def test_empty_secret_skips_verification(self):
        from webhooks import _verify_signature

        assert _verify_signature(b"payload", "", "") is True


class TestFormatPushEvent:
    def test_basic_push(self):
        from webhooks import _format_push_event

        data = {
            "ref": "refs/heads/main",
            "repository": {"full_name": "owner/repo"},
            "pusher": {"name": "alice"},
            "commits": [
                {"id": "abc1234567890", "message": "Fix bug"},
                {"id": "def4567890123", "message": "Add feature"},
            ],
        }
        result = _format_push_event(data)
        assert "owner/repo" in result
        assert "main" in result
        assert "alice" in result
        assert "abc1234" in result
        assert "Fix bug" in result

    def test_no_commits_returns_none(self):
        from webhooks import _format_push_event

        data = {"ref": "refs/heads/main", "repository": {"full_name": "r"}, "pusher": {"name": "a"}, "commits": []}
        assert _format_push_event(data) is None

    def test_truncates_at_5_commits(self):
        from webhooks import _format_push_event

        data = {
            "ref": "refs/heads/main",
            "repository": {"full_name": "r"},
            "pusher": {"name": "a"},
            "commits": [{"id": f"sha{i}", "message": f"msg{i}"} for i in range(8)],
        }
        result = _format_push_event(data)
        assert "3 more" in result


class TestFormatPREvent:
    def test_opened(self):
        from webhooks import _format_pr_event

        data = {
            "action": "opened",
            "pull_request": {
                "title": "Add feature",
                "number": 42,
                "user": {"login": "alice"},
                "html_url": "https://github.com/o/r/pull/42",
                "merged": False,
            },
            "repository": {"full_name": "o/r"},
        }
        result = _format_pr_event(data)
        assert "PR #42 opened" in result
        assert "Add feature" in result

    def test_merged(self):
        from webhooks import _format_pr_event

        data = {
            "action": "closed",
            "pull_request": {
                "title": "Fix",
                "number": 1,
                "user": {"login": "bob"},
                "html_url": "https://github.com/o/r/pull/1",
                "merged": True,
            },
            "repository": {"full_name": "o/r"},
        }
        result = _format_pr_event(data)
        assert "merged" in result

    def test_ignored_action(self):
        from webhooks import _format_pr_event

        data = {"action": "labeled", "pull_request": {}, "repository": {"full_name": "o/r"}}
        assert _format_pr_event(data) is None


class TestFormatIssueEvent:
    def test_opened(self):
        from webhooks import _format_issue_event

        data = {
            "action": "opened",
            "issue": {
                "title": "Bug report",
                "number": 10,
                "user": {"login": "carol"},
                "html_url": "https://github.com/o/r/issues/10",
            },
            "repository": {"full_name": "o/r"},
        }
        result = _format_issue_event(data)
        assert "Issue #10 opened" in result
        assert "Bug report" in result

    def test_ignored_action(self):
        from webhooks import _format_issue_event

        data = {"action": "labeled", "issue": {}, "repository": {"full_name": "o/r"}}
        assert _format_issue_event(data) is None


class TestFormatEvent:
    def test_unknown_event_type(self):
        from webhooks import _format_event

        assert _format_event("deployment", {}) is None

    def test_routes_push(self):
        from webhooks import _format_event

        data = {
            "ref": "refs/heads/main",
            "repository": {"full_name": "r"},
            "pusher": {"name": "a"},
            "commits": [{"id": "abc", "message": "msg"}],
        }
        result = _format_event("push", data)
        assert result is not None
