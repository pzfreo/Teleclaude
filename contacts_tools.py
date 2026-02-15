"""Google Contacts (People API) tools that Claude can call via tool_use."""

import json
import logging
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


class GoogleContactsClient:
    """Google People API client using OAuth2 refresh token."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        creds.refresh(Request())
        self.service = build("people", "v1", credentials=creds)

    def _format_person(self, person: dict) -> dict:
        """Extract useful fields from a Person resource."""
        names = person.get("names", [])
        emails = person.get("emailAddresses", [])
        phones = person.get("phoneNumbers", [])
        addresses = person.get("addresses", [])
        orgs = person.get("organizations", [])
        return {
            "resourceName": person.get("resourceName", ""),
            "name": names[0].get("displayName", "") if names else "",
            "givenName": names[0].get("givenName", "") if names else "",
            "familyName": names[0].get("familyName", "") if names else "",
            "emails": [e.get("value", "") for e in emails],
            "phones": [p.get("value", "") for p in phones],
            "addresses": [a.get("formattedValue", "") for a in addresses],
            "organizations": [{"name": o.get("name", ""), "title": o.get("title", "")} for o in orgs],
        }

    def search_contacts(self, query: str, page_size: int = 10) -> list[dict]:
        """Search contacts by name or other query."""
        results = (
            self.service.people()
            .searchContacts(
                query=query,
                readMask="names,emailAddresses,phoneNumbers,addresses,organizations",
                pageSize=page_size,
            )
            .execute()
        )
        return [self._format_person(r["person"]) for r in results.get("results", [])]

    def get_contact(self, resource_name: str) -> dict:
        """Get full details for a contact by resource name."""
        person = (
            self.service.people()
            .get(
                resourceName=resource_name,
                personFields="names,emailAddresses,phoneNumbers,addresses,organizations",
            )
            .execute()
        )
        return self._format_person(person)

    def create_contact(
        self,
        given_name: str,
        family_name: str = "",
        email: str = "",
        phone: str = "",
    ) -> dict:
        """Create a new contact."""
        body: dict[str, Any] = {
            "names": [{"givenName": given_name, "familyName": family_name}],
        }
        if email:
            body["emailAddresses"] = [{"value": email}]
        if phone:
            body["phoneNumbers"] = [{"value": phone}]
        person = self.service.people().createContact(body=body).execute()
        return self._format_person(person)

    def update_contact(
        self,
        resource_name: str,
        given_name: str | None = None,
        family_name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict:
        """Update an existing contact's fields."""
        # Fetch current contact to get etag
        person = (
            self.service.people()
            .get(
                resourceName=resource_name,
                personFields="names,emailAddresses,phoneNumbers",
            )
            .execute()
        )
        update_fields = []
        if given_name is not None or family_name is not None:
            names = person.get("names", [{}])
            name = names[0] if names else {}
            if given_name is not None:
                name["givenName"] = given_name
            if family_name is not None:
                name["familyName"] = family_name
            person["names"] = [name]
            update_fields.append("names")
        if email is not None:
            person["emailAddresses"] = [{"value": email}]
            update_fields.append("emailAddresses")
        if phone is not None:
            person["phoneNumbers"] = [{"value": phone}]
            update_fields.append("phoneNumbers")

        if not update_fields:
            return self._format_person(person)

        result = (
            self.service.people()
            .updateContact(
                resourceName=resource_name,
                body=person,
                updatePersonFields=",".join(update_fields),
            )
            .execute()
        )
        return self._format_person(result)

    def delete_contact(self, resource_name: str) -> str:
        """Delete a contact."""
        self.service.people().deleteContact(resourceName=resource_name).execute()
        return "Contact deleted."


CONTACTS_TOOLS = [
    {
        "name": "search_contacts",
        "description": "Search Google Contacts by name or query. Returns matching contacts with their details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (name, email, phone, etc.)"},
                "page_size": {
                    "type": "integer",
                    "description": "Max results to return (default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_contact",
        "description": "Get full details for a Google Contact by resource name (e.g. 'people/c12345').",
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_name": {
                    "type": "string",
                    "description": "Contact resource name (e.g. 'people/c12345')",
                },
            },
            "required": ["resource_name"],
        },
    },
    {
        "name": "create_contact",
        "description": "Create a new Google Contact.",
        "input_schema": {
            "type": "object",
            "properties": {
                "given_name": {"type": "string", "description": "First name"},
                "family_name": {"type": "string", "description": "Last name", "default": ""},
                "email": {"type": "string", "description": "Email address", "default": ""},
                "phone": {"type": "string", "description": "Phone number", "default": ""},
            },
            "required": ["given_name"],
        },
    },
    {
        "name": "update_contact",
        "description": "Update an existing Google Contact's name, email, or phone.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_name": {
                    "type": "string",
                    "description": "Contact resource name (e.g. 'people/c12345')",
                },
                "given_name": {"type": "string", "description": "New first name"},
                "family_name": {"type": "string", "description": "New last name"},
                "email": {"type": "string", "description": "New email address"},
                "phone": {"type": "string", "description": "New phone number"},
            },
            "required": ["resource_name"],
        },
    },
    {
        "name": "delete_contact",
        "description": "Delete a Google Contact.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_name": {
                    "type": "string",
                    "description": "Contact resource name (e.g. 'people/c12345')",
                },
            },
            "required": ["resource_name"],
        },
    },
]


def execute_tool(client: GoogleContactsClient, tool_name: str, tool_input: dict) -> str:
    """Execute a Google Contacts tool call."""
    try:
        result: Any
        if tool_name == "search_contacts":
            result = client.search_contacts(
                tool_input["query"],
                tool_input.get("page_size", 10),
            )
        elif tool_name == "get_contact":
            result = client.get_contact(tool_input["resource_name"])
        elif tool_name == "create_contact":
            result = client.create_contact(
                tool_input["given_name"],
                tool_input.get("family_name", ""),
                tool_input.get("email", ""),
                tool_input.get("phone", ""),
            )
        elif tool_name == "update_contact":
            result = client.update_contact(
                tool_input["resource_name"],
                given_name=tool_input.get("given_name"),
                family_name=tool_input.get("family_name"),
                email=tool_input.get("email"),
                phone=tool_input.get("phone"),
            )
        elif tool_name == "delete_contact":
            result = client.delete_contact(tool_input["resource_name"])
        else:
            return f"Unknown tool: {tool_name}"

        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Google Contacts error: {e}"
