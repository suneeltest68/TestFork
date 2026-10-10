from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from Dependencies.fyers_market_data import FyersMarketDataClient


def _symbol_masters() -> tuple[pd.DataFrame, pd.DataFrame]:
    expiry_timestamp = int(datetime(2026, 10, 13, 10, 0, tzinfo=UTC).timestamp())
    fyers_row = [""] * 17
    fyers_row[0] = "101123456789"
    fyers_row[8] = str(expiry_timestamp)
    fyers_row[9] = "NSE:NIFTY26O1310000CE"
    fyers_row[13] = "NIFTY"
    fyers_row[15] = "10000.0"
    fyers_row[16] = "CE"
    fyers = pd.DataFrame([fyers_row])
    dhan = pd.DataFrame(
        [
            {
                "SECURITY_ID": "98765",
                "UNDERLYING_SYMBOL": "NIFTY",
                "SM_EXPIRY_DATE": "2026-10-13",
                "STRIKE_PRICE": "10000",
                "OPTION_TYPE": "CE",
            }
        ]
    )
    return fyers, dhan


class _FakeFyers:
    def __init__(self) -> None:
        self.history_request = None
        self.quote_requests = []
        self.option_chain_request = None

    def get_profile(self):
        return {"s": "ok"}

    def history(self, *, data):
        self.history_request = data
        epoch = int(datetime(2026, 10, 9, 4, 15, tzinfo=UTC).timestamp())
        return {"s": "ok", "candles": [[epoch, 1, 2, 0.5, 1.5, 0]]}

    def quotes(self, *, data):
        self.quote_requests.append(data)
        symbol = data["symbols"].split(",")[0]
        return {"s": "ok", "d": [{"n": symbol, "s": "ok", "v": {"lp": 12.5}}]}

    def optionchain(self, *, data):
        self.option_chain_request = data
        return {
            "s": "ok",
            "data": {
                "underlyingValue": 10000,
                "optionsChain": [
                    {
                        "strike_price": 10000,
                        "option_type": "CE",
                        "ltp": 12.5,
                        "oi": 100,
                        "bid": 12.4,
                        "ask": 12.6,
                        "option_greeks": {
                            "iv": 20,
                            "delta": 0.5,
                            "gamma": 0.1,
                            "theta": -0.2,
                            "vega": 0.3,
                        },
                    }
                ],
            },
        }


def _client(tmp_path: Path, fake: _FakeFyers | None = None) -> FyersMarketDataClient:
    fyers_master, dhan_master = _symbol_masters()
    return FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        str(tmp_path / "all_instrument *.csv"),
        tmp_path / "fyers_nse_fo.csv",
        model=fake or _FakeFyers(),
        symbol_master_frame=fyers_master,
        dhan_master_frame=dhan_master,
    )


def test_fyers_history_maps_index_candles_to_runner_frame(tmp_path):
    fake = _FakeFyers()
    client = _client(tmp_path, fake)

    frame = client.fetch_index_1m_ohlc(13, "IDX_I", "INDEX")

    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert frame.iloc[0]["timestamp"] == pd.Timestamp("2026-10-09 09:45:00")
    assert fake.history_request["symbol"] == "NSE:NIFTY50-INDEX"
    assert fake.history_request["resolution"] == "1"


def test_access_token_rotation_validates_persists_and_advances_websocket_generation(tmp_path, monkeypatch):
    client = _client(tmp_path)
    candidate = _FakeFyers()
    monkeypatch.setattr(client, "_create_model", lambda _token: candidate)
    persisted = []

    client.replace_access_token("rotated-token", persist=persisted.append)

    assert persisted == ["rotated-token"]
    assert client.access_token == "rotated-token"
    assert client.token_version == 1
    client.validate_session()


def test_access_token_rotation_does_not_persist_or_swap_an_invalid_token(tmp_path, monkeypatch):
    client = _client(tmp_path)
    candidate = _FakeFyers()
    candidate.get_profile = lambda: {"s": "error", "message": "invalid token"}
    monkeypatch.setattr(client, "_create_model", lambda _token: candidate)
    persisted = []

    with pytest.raises(RuntimeError, match="Fyers access-token validation failed"):
        client.replace_access_token("invalid-token", persist=persisted.append)

    assert persisted == []
    assert client.access_token == "ACCESS-TOKEN"
    assert client.token_version == 0


def test_fyers_history_supports_five_second_option_candles(tmp_path):
    fake = _FakeFyers()
    client = FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        "",
        tmp_path / "fyers_nse_fo.csv",
        model=fake,
        load_symbol_mappings=False,
    )

    frame = client.fetch_history(
        "NSE:NIFTY26O1922300CE",
        date(2026, 10, 9),
        date(2026, 10, 9),
        resolution="5s",
    )

    assert len(frame) == 1
    assert fake.history_request == {
        "symbol": "NSE:NIFTY26O1922300CE",
        "resolution": "5S",
        "date_format": "1",
        "range_from": "2026-10-09",
        "range_to": "2026-10-09",
        "cont_flag": "1",
    }


def test_fyers_history_retries_transient_bad_request(tmp_path, monkeypatch):
    fake = _FakeFyers()
    original_history = fake.history
    responses = [{"s": "error", "message": "Bad request"}, None]

    def history(*, data):
        if responses[0] is not None:
            return responses.pop(0)
        return original_history(data=data)

    fake.history = history
    monkeypatch.setattr("Dependencies.fyers_market_data.time.sleep", lambda _seconds: None)
    client = FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        "",
        tmp_path / "fyers_nse_fo.csv",
        model=fake,
        load_symbol_mappings=False,
    )

    frame = client.fetch_history(
        "NSE:NIFTY26O1322650PE",
        date(2026, 9, 30),
        date(2026, 9, 30),
        resolution=1,
    )

    assert len(frame) == 1
    assert fake.history_request["symbol"] == "NSE:NIFTY26O1322650PE"


def test_fyers_history_rejects_unsupported_second_interval(tmp_path):
    client = FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        "",
        tmp_path / "fyers_nse_fo.csv",
        model=_FakeFyers(),
        load_symbol_mappings=False,
    )

    with pytest.raises(ValueError, match="Unsupported Fyers history resolution"):
        client.fetch_history(
            "NSE:NIFTY26O1922300CE",
            date(2026, 10, 9),
            date(2026, 10, 9),
            resolution="10S",
        )


def test_fyers_expired_fno_methods_use_documented_get_endpoints(tmp_path, monkeypatch):
    responses = [
        {
            "s": "ok",
            "data": {"expiry_dates": {"options": ["2026-05-28", "2026-05-07"]}},
        },
        {
            "s": "ok",
            "data": {"contracts": {"options": ["NSE:NIFTY26MAY22000CE"]}},
        },
        {
            "s": "ok",
            "columns": ["timestamp", "open", "high", "low", "close", "volume", "open_interest"],
            "candles": [[1778228100, 100, 105, 98, 102, 500, 1200]],
        },
    ]
    requests_made = []

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, *, params, headers, timeout):
        requests_made.append((url, params, headers, timeout))
        return _Response(responses.pop(0))

    monkeypatch.setattr("Dependencies.fyers_market_data.requests.get", fake_get)
    client = FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        "",
        tmp_path / "fyers_nse_fo.csv",
        model=_FakeFyers(),
        load_symbol_mappings=False,
    )

    expiries = client.fetch_expiry_dates("NSE:NIFTY50-INDEX", date(2026, 5, 1), date(2026, 6, 15))
    symbols = client.fetch_expired_option_symbols("NSE:NIFTY50-INDEX", date(2026, 5, 28))
    history = client.fetch_expired_option_history(symbols[0], date(2026, 5, 7), date(2026, 5, 7), resolution=1)

    assert expiries == [date(2026, 5, 7), date(2026, 5, 28)]
    assert symbols == ["NSE:NIFTY26MAY22000CE"]
    assert history.iloc[0].to_dict() == {
        "timestamp": 1778228100,
        "open": 100,
        "high": 105,
        "low": 98,
        "close": 102,
        "volume": 500,
    }
    assert [request[0] for request in requests_made] == [
        "https://api-t1.fyers.in/data/history/fno/expired/expiry-dates",
        "https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols",
        "https://api-t1.fyers.in/data/history/fno/expired/historical-data",
    ]
    assert requests_made[0][1]["range_to"] == "2026-06-15"
    assert requests_made[2][1]["resolution"] == "1"
    assert requests_made[0][2]["Authorization"] == "APP-ID:ACCESS-TOKEN"
    assert all(request[3] == 10.0 for request in requests_made)


def test_fyers_expired_history_treats_no_data_as_empty(tmp_path, monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "s": "no_data",
                "columns": ["timestamp", "open", "high", "low", "close", "volume"],
                "candles": [],
            }

    monkeypatch.setattr(
        "Dependencies.fyers_market_data.requests.get",
        lambda *_args, **_kwargs: _Response(),
    )
    client = FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        "",
        tmp_path / "fyers_nse_fo.csv",
        model=_FakeFyers(),
        load_symbol_mappings=False,
    )

    frame = client.fetch_expired_option_history(
        "NSE:NIFTY26MAY22000CE",
        date(2026, 5, 7),
        date(2026, 5, 7),
        resolution=1,
    )

    assert frame.empty
    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_quotes_translate_dhan_security_ids_to_fyers_symbols(tmp_path):
    fake = _FakeFyers()
    client = _client(tmp_path, fake)

    result = client.fetch_ltp_map({"NSE_FNO": [98765]})

    assert result == {("NSE_FNO", 98765): 12.5}
    assert fake.quote_requests == [{"symbols": "NSE:NIFTY26O1310000CE"}]


def test_option_chain_normalizes_fyers_fields_to_runner_contract(tmp_path):
    fake = _FakeFyers()
    client = _client(tmp_path, fake)

    result = client.fetch_option_chain(13, "IDX_I", date(2026, 10, 13))

    node = result["data"]["oc"]["10000.0000"]["ce"]
    assert result["data"]["last_price"] == 10000
    assert node["last_price"] == 12.5
    assert node["open_interest"] == 100
    assert node["implied_volatility"] == 20
    assert node["top_bid_price"] == 12.4
    assert node["greeks"]["delta"] == 0.5
    assert fake.option_chain_request["timestamp"].isdigit()
    assert fake.option_chain_request["greeks"] == "1"


def test_unknown_security_id_fails_instead_of_returning_an_empty_quote(tmp_path):
    client = _client(tmp_path)

    with pytest.raises(KeyError, match="No Fyers symbol"):
        client.fetch_ltp_map({"NSE_FNO": [999999]})


def test_security_id_cannot_be_used_with_the_wrong_segment(tmp_path):
    client = _client(tmp_path)

    with pytest.raises(KeyError, match="No Fyers symbol is mapped for IDX_I"):
        client.fetch_ltp_map({"IDX_I": [98765]})


def test_invalid_fyers_credentials_are_rejected_before_client_creation(tmp_path):
    fyers_master, dhan_master = _symbol_masters()

    with pytest.raises(ValueError, match="FYERS_CLIENT_ID"):
        FyersMarketDataClient(
            "",
            "token",
            str(tmp_path / "all_instrument *.csv"),
            tmp_path / "fyers_nse_fo.csv",
            model=_FakeFyers(),
            symbol_master_frame=fyers_master,
            dhan_master_frame=dhan_master,
        )


def test_missing_fyers_token_is_only_allowed_for_deferred_auth(tmp_path):
    fyers_master, dhan_master = _symbol_masters()

    with pytest.raises(ValueError, match="FYERS_ACCESS_TOKEN"):
        FyersMarketDataClient(
            "APP-ID",
            "",
            str(tmp_path / "all_instrument *.csv"),
            tmp_path / "fyers_nse_fo.csv",
            symbol_master_frame=fyers_master,
            dhan_master_frame=dhan_master,
        )

    client = FyersMarketDataClient(
        "APP-ID",
        "",
        str(tmp_path / "all_instrument *.csv"),
        tmp_path / "fyers_nse_fo.csv",
        symbol_master_frame=fyers_master,
        dhan_master_frame=dhan_master,
        allow_missing_access_token=True,
    )
    with pytest.raises(RuntimeError, match="No Fyers access token"):
        client.validate_session()


def test_fyers_sdk_requests_receive_a_finite_default_timeout(tmp_path):
    fyers_master, dhan_master = _symbol_masters()
    original_request = Mock(return_value="response")
    fake = SimpleNamespace(service=SimpleNamespace(session=SimpleNamespace(request=original_request)))
    client = FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        "",
        tmp_path / "fyers_nse_fo.csv",
        model=fake,
        symbol_master_frame=fyers_master,
        dhan_master_frame=dhan_master,
        request_timeout_seconds=4.0,
    )

    assert fake.service.session.request("GET", "https://example.test") == "response"
    original_request.assert_called_once_with("GET", "https://example.test", timeout=4.0)
    assert client.request_timeout_seconds == 4.0


def test_explicit_fyers_request_timeout_is_preserved(tmp_path):
    fyers_master, dhan_master = _symbol_masters()
    original_request = Mock(return_value="response")
    fake = SimpleNamespace(service=SimpleNamespace(session=SimpleNamespace(request=original_request)))
    FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        "",
        tmp_path / "fyers_nse_fo.csv",
        model=fake,
        symbol_master_frame=fyers_master,
        dhan_master_frame=dhan_master,
    )

    fake.service.session.request("GET", "https://example.test", timeout=2.0)
    original_request.assert_called_once_with("GET", "https://example.test", timeout=2.0)


def test_missing_runner_contract_master_is_downloaded_from_public_master(tmp_path, monkeypatch):
    fyers_master, dhan_master = _symbol_masters()
    master_csv = dhan_master.to_csv(index=False).encode()

    class _Response:
        content = master_csv

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self.content

        def close(self):
            pass

    monkeypatch.setattr(
        "Dependencies.fyers_market_data.requests.get",
        lambda *args, **kwargs: _Response(),
    )
    client = FyersMarketDataClient(
        "APP-ID",
        "ACCESS-TOKEN",
        str(tmp_path / "all_instrument *.csv"),
        tmp_path / "fyers_nse_fo.csv",
        model=_FakeFyers(),
        symbol_master_frame=fyers_master,
    )

    assert client.fetch_ltp_map({"NSE_FNO": [98765]}) == {("NSE_FNO", 98765): 12.5}
    assert list(tmp_path.glob("all_instrument *.csv"))
