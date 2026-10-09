from __future__ import annotations

import hashlib
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from Dependencies import fyers_token_setup as setup


def test_authorization_url_carries_required_oauth_parameters() -> None:
    url = setup.authorization_url("app-id", "https://localhost/callback", "state")
    query = parse_qs(urlparse(url).query)

    assert url.startswith(setup.AUTH_URL)
    assert query == {
        "client_id": ["app-id"],
        "redirect_uri": ["https://localhost/callback"],
        "response_type": ["code"],
        "state": ["state"],
    }


def test_extract_authorization_code_checks_returned_state() -> None:
    callback = "https://localhost/callback?auth_code=temporary-code&state=expected"

    assert setup.extract_authorization_code(callback, "expected") == "temporary-code"
    with pytest.raises(ValueError, match="state"):
        setup.extract_authorization_code(callback, "different")


def test_exchange_hashes_app_id_and_secret_and_returns_token(monkeypatch) -> None:
    response = Mock()
    response.json.return_value = {"s": "ok", "access_token": "new-token"}
    response.raise_for_status.return_value = None
    post = Mock(return_value=response)
    monkeypatch.setattr(setup.requests, "post", post)

    token = setup.exchange_authorization_code("app-id", "private-secret", "auth-code")

    expected_hash = hashlib.sha256(b"app-id:private-secret").hexdigest()
    post.assert_called_once_with(
        setup.TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "appIdHash": expected_hash,
            "code": "auth-code",
        },
        timeout=setup.REQUEST_TIMEOUT_SECONDS,
    )
    assert token == "new-token"


def test_write_access_token_preserves_other_env_content(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# Fyers settings\nFYERS_CLIENT_ID=app-id\n"
        "FYERS_ACCESS_TOKEN=old-token\nOTHER=value\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup, "ENV_PATH", env_path)

    setup.write_access_token("fresh-token")

    assert env_path.read_text(encoding="utf-8") == (
        "# Fyers settings\nFYERS_CLIENT_ID=app-id\n"
        "FYERS_ACCESS_TOKEN=fresh-token\nOTHER=value\n"
    )


def test_cli_dispatches_fyers_token_setup(monkeypatch) -> None:
    import algo

    run = Mock(return_value=0)
    monkeypatch.setattr(algo, "_run", run)

    assert algo.main(["setup-fyers-token"]) == 0
    run.assert_called_once_with("Dependencies/fyers_token_setup.py", [])
