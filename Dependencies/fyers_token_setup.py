"""Interactive Fyers OAuth setup for the market-data access token."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / ".env"
AUTH_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
REQUEST_TIMEOUT_SECONDS = 10


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the Fyers authorization URL for the configured application."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def extract_authorization_code(redirected_url: str, expected_state: str) -> str:
    """Extract the short-lived authorization code and validate the OAuth state."""
    parsed_url = urlparse(redirected_url.strip())
    values = parse_qs(parsed_url.query)
    codes = values.get("auth_code", [])
    states = values.get("state", [])
    if not codes or not codes[0].strip():
        raise ValueError("The pasted redirect URL does not contain an auth_code.")
    if not states or not secrets.compare_digest(states[0], expected_state):
        raise ValueError("The OAuth state did not match; restart token setup.")
    return codes[0].strip()


def exchange_authorization_code(
    client_id: str,
    secret_key: str,
    authorization_code: str,
) -> str:
    """Exchange Fyers' authorization code for an access token."""
    app_id_hash = hashlib.sha256(
        f"{client_id}:{secret_key}".encode()
    ).hexdigest()
    response = requests.post(
        TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": authorization_code,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or str(payload.get("s", "")).lower() != "ok":
        raise RuntimeError("Fyers rejected the authorization code or returned an invalid token response.")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError("Fyers response did not include an access token.")
    return access_token.strip()


def write_access_token(access_token: str) -> None:
    """Replace FYERS_ACCESS_TOKEN in .env without changing other lines."""
    if not ENV_PATH.is_file():
        raise FileNotFoundError(
            f"{ENV_PATH} does not exist. Copy Dependencies/env.example to Dependencies/.env first."
        )
    raw = ENV_PATH.read_text(encoding="utf-8")
    lines = raw.splitlines()
    key_prefix = "FYERS_ACCESS_TOKEN="
    found = False
    updated: list[str] = []
    for line in lines:
        key, separator, _ = line.partition("=")
        if separator and key.strip() == "FYERS_ACCESS_TOKEN":
            updated.append(f"{key_prefix}{access_token}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"{key_prefix}{access_token}")
    text = "\n".join(updated)
    if raw.endswith("\n"):
        text += "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    """Run the browser-assisted Fyers authorization flow."""
    if not ENV_PATH.is_file():
        print(
            f"ERROR: .env not found at {ENV_PATH}\n"
            "Copy Dependencies/env.example to Dependencies/.env, then add "
            "FYERS_CLIENT_ID, FYERS_SECRET_KEY, and FYERS_REDIRECT_URI."
        )
        return 1

    load_dotenv(dotenv_path=ENV_PATH, override=False)
    client_id = (os.getenv("FYERS_CLIENT_ID") or "").strip().strip("\"'")
    secret_key = (os.getenv("FYERS_SECRET_KEY") or "").strip().strip("\"'")
    redirect_uri = (os.getenv("FYERS_REDIRECT_URI") or "").strip().strip("\"'")
    missing = [
        name
        for name, value in (
            ("FYERS_CLIENT_ID", client_id),
            ("FYERS_SECRET_KEY", secret_key),
            ("FYERS_REDIRECT_URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        print(
            "ERROR: Set these values in Dependencies/.env before continuing: "
            + ", ".join(missing)
        )
        return 1

    state = secrets.token_urlsafe(24)
    print("Open this Fyers authorization URL in your browser and approve access:\n")
    print(authorization_url(client_id, redirect_uri, state))
    print(
        "\nAfter approval, paste the complete redirected URL below. "
        "The redirect URI must be registered in your Fyers developer app."
    )
    try:
        redirected_url = input("Redirect URL: ").strip()
        authorization_code = extract_authorization_code(redirected_url, state)
        access_token = exchange_authorization_code(
            client_id, secret_key, authorization_code
        )
        write_access_token(access_token)
    except (EOFError, KeyboardInterrupt):
        print("\nToken setup cancelled.")
        return 1
    except requests.RequestException as exc:
        print(
            "ERROR: Fyers token setup failed during the HTTP request "
            f"({type(exc).__name__}). Check connectivity and try again."
        )
        return 1
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: Fyers token setup failed: {exc}")
        return 1

    print(f"Fyers access token saved to {ENV_PATH}. Keep Dependencies/.env private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
