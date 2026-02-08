#!/usr/bin/env python3
"""One-time setup to authorize Google Tasks access.

Run this locally to get a refresh token, then add it to Railway.

Usage:
    python setup_google.py

Prerequisites:
    1. Go to https://console.cloud.google.com/
    2. Create a project (or use an existing one)
    3. Enable the Google Tasks API
    4. Go to Credentials -> Create Credentials -> OAuth 2.0 Client ID
    5. Choose "Desktop app" as the application type
    6. Download the JSON and save it as 'credentials.json' in this directory
       OR set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in your .env
"""

import json
import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/tasks"]


def main():
    # Try credentials.json first, fall back to env vars
    if os.path.exists("credentials.json"):
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    else:
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        if not client_id or not client_secret:
            print("Error: No credentials.json found and GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set.")
            print("See the instructions at the top of this file.")
            return

        flow = InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            },
            SCOPES,
        )

    creds = flow.run_local_server(port=0)

    print("\n--- Add these to your Railway environment variables ---\n")
    print(f"GOOGLE_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print("\n--- Done! ---")


if __name__ == "__main__":
    main()
