from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

from Dependencies import fyers_telegram_auth as auth


class _FakeMarketDataClient:
    def __init__(self) -> None:
        self.installed_token = None

    def replace_access_token(self, token, *, persist):
        persist(token)
        self.installed_token = token

    def validate_session(self):
        return None


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _service(tmp_path: Path) -> auth.FyersTelegramAuthService:
    return auth.FyersTelegramAuthService(
        bot_token="bot-token",
        authorized_chat_id="12345",
        client_id="app-id",
        secret_key="app-secret",
        redirect_uri="https://service.example/callback/",
        token_file=tmp_path / "fyers_access_token",
        market_data_client=_FakeMarketDataClient(),
        port=_unused_port(),
    )


def test_persisted_access_token_takes_precedence_over_environment(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("renewed-token\n", encoding="utf-8")

    assert auth.load_persisted_access_token(token_file, "old-token") == "renewed-token"
    assert auth.load_persisted_access_token(tmp_path / "missing", "env-token") == "env-token"


def test_auth_callback_checks_state_exchanges_code_and_persists_token(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service._pending_state = "expected-state"
    service._pending_state_expires_at = 10**20
    service._notify = Mock()
    exchange = Mock(return_value="new-access-token")
    monkeypatch.setattr(auth, "exchange_authorization_code", exchange)
    redact_secret = Mock()
    monkeypatch.setattr(auth, "add_redaction_secrets", redact_secret)

    service._handle_callback(
        "/callback/?s=ok&code=200&auth_code=temporary-code&state=expected-state"
    )

    exchange.assert_called_once_with("app-id", "app-secret", "temporary-code")
    assert service.market_data_client.installed_token == "new-access-token"
    assert (tmp_path / "fyers_access_token").read_text(encoding="utf-8") == "new-access-token"
    assert service._token_updated.is_set()
    assert service._pending_state is None
    redact_secret.assert_called_once_with(auth.logging.getLogger(), ("new-access-token",))
    service._httpd.server_close()


def test_auth_callback_rejects_wrong_or_replayed_state(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service._pending_state = "expected-state"
    service._pending_state_expires_at = 10**20
    exchange = Mock()
    monkeypatch.setattr(auth, "exchange_authorization_code", exchange)

    callback = "/callback/?auth_code=temporary-code&state=wrong"
    with pytest.raises(ValueError, match="state"):
        service._handle_callback(callback)

    service._httpd.server_close()
    exchange.assert_not_called()


@pytest.mark.parametrize(
    ("chat_type", "chat_id", "accepted"),
    [
        ("private", 12345, True),
        ("private", 54321, False),
        ("group", 12345, False),
    ],
)
def test_only_configured_private_chat_can_request_auth(
    tmp_path,
    chat_type,
    chat_id,
    accepted,
):
    service = _service(tmp_path)
    request_auth = Mock()
    service.request_authorization = request_auth

    service._handle_update(
        {
            "message": {
                "chat": {"id": chat_id, "type": chat_type},
                "text": "/auth@trade_bot",
            }
        }
    )

    assert request_auth.called is accepted
    service._httpd.server_close()


def test_auth_service_requires_https_callback_and_numeric_private_chat(tmp_path):
    client = _FakeMarketDataClient()
    common = {
        "bot_token": "bot-token",
        "authorized_chat_id": "12345",
        "client_id": "app-id",
        "secret_key": "app-secret",
        "redirect_uri": "https://service.example/callback/",
        "token_file": tmp_path / "token",
        "market_data_client": client,
        "port": _unused_port(),
    }

    with pytest.raises(ValueError, match="HTTPS"):
        auth.FyersTelegramAuthService(**(common | {"redirect_uri": "http://service.example/callback"}))
    with pytest.raises(ValueError, match="numeric private-chat"):
        auth.FyersTelegramAuthService(**(common | {"authorized_chat_id": "@channel"}))
