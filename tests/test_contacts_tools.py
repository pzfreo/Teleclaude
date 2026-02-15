"""Tests for contacts_tools.py — Google Contacts client and tool dispatch."""

import json
from unittest.mock import MagicMock, patch


class TestGoogleContactsClient:
    def _make_client(self):
        """Create a GoogleContactsClient with mocked credentials."""
        with (
            patch("contacts_tools.Credentials") as mock_creds_cls,
            patch("contacts_tools.Request"),
            patch("contacts_tools.build") as mock_build,
        ):
            mock_creds = MagicMock()
            mock_creds_cls.return_value = mock_creds
            mock_service = MagicMock()
            mock_build.return_value = mock_service

            from contacts_tools import GoogleContactsClient

            client = GoogleContactsClient("cid", "csec", "rtok")
            return client, mock_service

    def test_search_contacts(self):
        client, svc = self._make_client()
        svc.people().searchContacts().execute.return_value = {
            "results": [
                {
                    "person": {
                        "resourceName": "people/c123",
                        "names": [{"displayName": "John Doe", "givenName": "John", "familyName": "Doe"}],
                        "emailAddresses": [{"value": "john@example.com"}],
                        "phoneNumbers": [{"value": "+1234567890"}],
                        "addresses": [],
                        "organizations": [],
                    }
                }
            ]
        }
        result = client.search_contacts("John")
        assert len(result) == 1
        assert result[0]["name"] == "John Doe"
        assert result[0]["emails"] == ["john@example.com"]
        assert result[0]["phones"] == ["+1234567890"]

    def test_search_contacts_empty(self):
        client, svc = self._make_client()
        svc.people().searchContacts().execute.return_value = {}
        result = client.search_contacts("nobody")
        assert result == []

    def test_get_contact(self):
        client, svc = self._make_client()
        svc.people().get().execute.return_value = {
            "resourceName": "people/c123",
            "names": [{"displayName": "Jane Smith", "givenName": "Jane", "familyName": "Smith"}],
            "emailAddresses": [{"value": "jane@example.com"}],
            "phoneNumbers": [],
            "addresses": [{"formattedValue": "123 Main St"}],
            "organizations": [{"name": "Acme", "title": "Engineer"}],
        }
        result = client.get_contact("people/c123")
        assert result["name"] == "Jane Smith"
        assert result["addresses"] == ["123 Main St"]
        assert result["organizations"][0]["name"] == "Acme"

    def test_create_contact(self):
        client, svc = self._make_client()
        svc.people().createContact().execute.return_value = {
            "resourceName": "people/c456",
            "names": [{"displayName": "New Person", "givenName": "New", "familyName": "Person"}],
            "emailAddresses": [{"value": "new@example.com"}],
            "phoneNumbers": [],
            "addresses": [],
            "organizations": [],
        }
        result = client.create_contact("New", "Person", "new@example.com")
        assert result["resourceName"] == "people/c456"
        assert result["name"] == "New Person"

    def test_update_contact(self):
        client, svc = self._make_client()
        svc.people().get().execute.return_value = {
            "resourceName": "people/c123",
            "etag": "abc",
            "names": [{"givenName": "John", "familyName": "Doe"}],
            "emailAddresses": [{"value": "old@example.com"}],
            "phoneNumbers": [],
        }
        svc.people().updateContact().execute.return_value = {
            "resourceName": "people/c123",
            "names": [{"displayName": "John Doe", "givenName": "John", "familyName": "Doe"}],
            "emailAddresses": [{"value": "new@example.com"}],
            "phoneNumbers": [],
            "addresses": [],
            "organizations": [],
        }
        result = client.update_contact("people/c123", email="new@example.com")
        assert result["emails"] == ["new@example.com"]

    def test_update_contact_no_changes(self):
        client, svc = self._make_client()
        svc.people().get().execute.return_value = {
            "resourceName": "people/c123",
            "names": [{"displayName": "John", "givenName": "John", "familyName": ""}],
            "emailAddresses": [],
            "phoneNumbers": [],
            "addresses": [],
            "organizations": [],
        }
        result = client.update_contact("people/c123")
        assert result["name"] == "John"

    def test_delete_contact(self):
        client, svc = self._make_client()
        svc.people().deleteContact().execute.return_value = None
        result = client.delete_contact("people/c123")
        assert result == "Contact deleted."


class TestExecuteTool:
    def test_search_contacts(self):
        from contacts_tools import execute_tool

        client = MagicMock()
        client.search_contacts.return_value = [{"name": "John", "resourceName": "people/c1"}]
        result = execute_tool(client, "search_contacts", {"query": "John"})
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "John"

    def test_get_contact(self):
        from contacts_tools import execute_tool

        client = MagicMock()
        client.get_contact.return_value = {"name": "Jane", "resourceName": "people/c2"}
        result = execute_tool(client, "get_contact", {"resource_name": "people/c2"})
        parsed = json.loads(result)
        assert parsed["name"] == "Jane"

    def test_create_contact(self):
        from contacts_tools import execute_tool

        client = MagicMock()
        client.create_contact.return_value = {"name": "New", "resourceName": "people/c3"}
        result = execute_tool(client, "create_contact", {"given_name": "New"})
        parsed = json.loads(result)
        assert parsed["resourceName"] == "people/c3"

    def test_update_contact(self):
        from contacts_tools import execute_tool

        client = MagicMock()
        client.update_contact.return_value = {"name": "Updated", "resourceName": "people/c1"}
        result = execute_tool(client, "update_contact", {"resource_name": "people/c1", "given_name": "Updated"})
        parsed = json.loads(result)
        assert parsed["name"] == "Updated"

    def test_delete_contact(self):
        from contacts_tools import execute_tool

        client = MagicMock()
        client.delete_contact.return_value = "Contact deleted."
        result = execute_tool(client, "delete_contact", {"resource_name": "people/c1"})
        assert result == "Contact deleted."

    def test_unknown_tool(self):
        from contacts_tools import execute_tool

        client = MagicMock()
        result = execute_tool(client, "nonexistent", {})
        assert "Unknown tool" in result

    def test_error_handling(self):
        from contacts_tools import execute_tool

        client = MagicMock()
        client.search_contacts.side_effect = RuntimeError("API failure")
        result = execute_tool(client, "search_contacts", {"query": "test"})
        assert "Google Contacts error" in result
