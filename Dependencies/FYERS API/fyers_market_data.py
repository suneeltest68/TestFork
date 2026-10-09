"""FYERS market-data adapter for the shared runner.

The runner consumes legacy (segment, numeric-id) keys; this adapter translates
those keys into exact FYERS symbols and emits the existing Dhan-shaped internal
tick envelope. No Dhan live API is called by this adapter.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws
import pandas as pd
import pytz

from fyers_common import FyersSymbolMaster, legacy_instrument_csv

IST = pytz.timezone("Asia/Kolkata")
LOG = logging.getLogger(__name__)

def _required_env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"{key} must be configured in Dependencies/.env")
    return value

def _ok(response: Any, operation: str) -> dict:
    if not isinstance(response, dict) or str(response.get("s", "")).lower() != "ok":
        raise RuntimeError(f"FYERS {operation} failed: {response!r}")
    return response

class FyersMarketDataClient:
    def __init__(self, app_id: str | None = None, access_token: str | None = None):
        self.app_id = (app_id or _required_env("FYERS_APP_ID")).strip()
        self.access_token = (access_token or _required_env("FYERS_ACCESS_TOKEN")).strip()
        self.api = fyersModel.FyersModel(
            client_id=self.app_id, token=self.access_token, is_async=False, log_path=""
        )
        self.symbols = FyersSymbolMaster()
        self._index_symbols: dict[str, str] = {}

    def validate_session(self) -> dict:
        return _ok(self.api.get_profile(), "profile")

    @staticmethod
    def _index_alias(security_id: int, instrument_type: str = "") -> str:
        return {13: "NIFTY", 25: "BANKNIFTY", 27: "FINNIFTY"}.get(
            int(security_id), str(instrument_type or "NIFTY").upper()
        )

    def _symbol_for_key(self, segment: str, security_id: int) -> str:
        if str(segment).upper() == "IDX_I":
            alias = self._index_alias(security_id)
            if alias not in self._index_symbols:
                self._index_symbols[alias] = self.symbols.index_symbol(alias)
            return self._index_symbols[alias]
        # Options are resolved from the legacy instrument master and mapped to
        # FYERS by exact expiry/strike/right in the execution adapter. Unknown
        # numeric ids must fail closed rather than be interpreted as FYERS tokens.
        path = legacy_instrument_csv()
        if path is None or not path.is_file():
            raise LookupError(
                "FYERS option lookup requires FYERS_LEGACY_INSTRUMENT_CSV pointing "
                "to the existing Dhan-format instrument CSV"
            )
        import csv
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("SECURITY_ID", "")).strip() == str(security_id):
                    return self.symbols.option_symbol(
                        str(row.get("UNDERLYING_SYMBOL", "NIFTY")),
                        datetime.strptime(str(row["SM_EXPIRY_DATE"])[:10], "%Y-%m-%d").date(),
                        float(row["STRIKE_PRICE"]), str(row["OPTION_TYPE"])
                    )
        raise LookupError(f"Legacy security id {security_id} not found in instrument CSV")

    def fetch_index_1m_ohlc(self, security_id: int, exchange_segment: str,
                            instrument_type: str, lookback_days: int = 7) -> pd.DataFrame:
        alias = self._index_alias(security_id, instrument_type)
        symbol = self._symbol_for_key("IDX_I", security_id)
        end = datetime.now(IST).date()
        start = end - timedelta(days=max(1, int(lookback_days)))
        resp = _ok(self.api.history(data={
            "symbol": symbol, "resolution": "1", "date_format": "1",
            "range_from": start.isoformat(), "range_to": end.isoformat(), "cont_flag": "1"
        }), "history")
        rows = resp.get("candles")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"FYERS returned no 1-minute candles for {alias}")
        df = pd.DataFrame(rows, columns=["epoch", "open", "high", "low", "close", "volume"])
        ts = pd.to_datetime(df.pop("epoch"), unit="s", utc=True).dt.tz_convert(IST).dt.tz_localize(None)
        df.insert(0, "timestamp", ts)
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["timestamp", "open", "high", "low", "close"]].dropna().sort_values("timestamp")

    def fetch_ltp_map(self, securities_by_segment: dict[str, list[int]]) -> dict[tuple[str, int], float]:
        translated: dict[str, tuple[str, int]] = {}
        for segment, ids in (securities_by_segment or {}).items():
            for sid in ids:
                translated[self._symbol_for_key(segment, int(sid))] = (str(segment), int(sid))
        out: dict[tuple[str, int], float] = {}
        symbols = list(translated)
        for offset in range(0, len(symbols), 50):
            batch = symbols[offset:offset + 50]
            response = _ok(self.api.quotes(data={"symbols": ",".join(batch)}), "quotes")
            for item in response.get("d", []):
                name, values = item.get("n"), item.get("v", {})
                key = translated.get(str(name))
                try:
                    ltp = float(values.get("lp", 0))
                except (TypeError, ValueError):
                    continue
                if key and ltp > 0:
                    out[key] = ltp
        return out

    def fetch_option_chain(self, under_security_id: int, under_exchange_segment: str, expiry) -> dict:
        underlying = self._index_alias(under_security_id)
        symbol = self._symbol_for_key("IDX_I", under_security_id)
        # FYERS accepts an epoch timestamp for expiry selection; use local midnight.
        expiry_epoch = int(IST.localize(datetime.combine(expiry, datetime.min.time())).timestamp())
        resp = _ok(self.api.optionchain(data={
            "symbol": symbol, "strikecount": 50, "timestamp": expiry_epoch, "greeks": True
        }), "option chain")
        payload = resp.get("data", {})
        chain: dict[str, dict] = {}
        spot = 0.0
        for item in payload.get("optionsChain", []):
            try:
                strike = float(item.get("strike_price", 0))
                ltp = float(item.get("ltp", 0) or 0)
            except (TypeError, ValueError):
                continue
            right = str(item.get("option_type", "")).upper()
            if not right:
                spot = ltp or spot
                continue
            if right not in ("CE", "PE"):
                continue
            side = "ce" if right == "CE" else "pe"
            chain.setdefault(str(strike), {})[side] = {
                "last_price": ltp, "implied_volatility": item.get("iv", 0),
                "greeks": {"delta": item.get("delta"), "gamma": item.get("gamma"),
                           "theta": item.get("theta"), "vega": item.get("vega")},
            }
        return {"status": "success", "data": {"last_price": spot, "oc": chain}}

    def make_market_feed(self, instruments: list[tuple[int, str, int]]):
        return FyersFeedBridge(self, instruments)

class FyersFeedBridge:
    """Expose the current feed's queue/subscription interface over FYERS data_ws."""
    def __init__(self, client: FyersMarketDataClient, instruments):
        self.client = client
        self.initial = list(instruments)
        self.queue: queue.Queue = queue.Queue(maxsize=20000)
        self.socket = None
        self.thread = None
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.error: BaseException | None = None
        self._lock = threading.RLock()
        self._subscribed: dict[tuple[str, int], str] = {}
        for seg_code, sid, _ in self.initial:
            segment = "IDX_I" if int(seg_code) == 0 else "NSE_FNO"
            self._subscribed[(segment, int(sid))] = client._symbol_for_key(segment, int(sid))

    def _on_message(self, message):
        if not isinstance(message, dict):
            return
        symbol = str(message.get("symbol", message.get("n", "")))
        try:
            price = float(message.get("ltp", message.get("lp", message.get("v", {}).get("lp", 0))))
        except (TypeError, ValueError, AttributeError):
            return
        if price <= 0:
            return
        key = next((k for k, v in self._subscribed.items() if v == symbol), None)
        if key is None:
            return
        packet = {"type": "Ticker Data", "exchange_segment": 0 if key[0] == "IDX_I" else 2,
                  "security_id": str(key[1]), "LTP": str(price),
                  "LTT": datetime.now(IST).strftime("%H:%M:%S")}
        try:
            self.queue.put_nowait(packet)
        except queue.Full:
            LOG.error("FYERS market-data queue full; dropping tick")
    def _on_error(self, *args):
        self.error = RuntimeError(f"FYERS data websocket error: {args!r}")
        self.closed.set()
    def _on_close(self, *args):
        self.closed.set()
    def _on_open(self):
        self.connected.set()
        with self._lock:
            symbols = list(dict.fromkeys(self._subscribed.values()))
        if symbols:
            self.socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
        self.socket.keep_running()
    def run_forever(self):
        self.socket = data_ws.FyersDataSocket(
            access_token=f"{self.client.app_id}:{self.client.access_token}",
            log_path="", litemode=False, write_to_file=False, reconnect=True,
            on_connect=self._on_open, on_message=self._on_message,
            on_error=self._on_error, on_close=self._on_close
        )
        self.thread = threading.Thread(target=self.socket.connect, name="fyers-data-ws", daemon=True)
        self.thread.start()
        if not self.connected.wait(timeout=15):
            raise RuntimeError("FYERS data websocket did not connect within 15 seconds")
    def get_data(self):
        if self.error:
            raise self.error
        try:
            return self.queue.get(timeout=0.5)
        except queue.Empty:
            if self.closed.is_set():
                raise RuntimeError("FYERS data websocket closed")
            return None
    def subscribe_symbols(self, instruments):
        with self._lock:
            for seg_code, sid, _ in instruments:
                segment = "IDX_I" if int(seg_code) == 0 else "NSE_FNO"
                key = (segment, int(sid))
                self._subscribed[key] = self.client._symbol_for_key(*key)
            if self.connected.is_set() and self.socket:
                self.socket.subscribe(symbols=list(dict.fromkeys(
                    self.client._symbol_for_key("IDX_I" if int(i[0]) == 0 else "NSE_FNO", int(i[1]))
                    for i in instruments)), data_type="SymbolUpdate")
    def unsubscribe_symbols(self, instruments):
        symbols = []
        with self._lock:
            for seg_code, sid, _ in instruments:
                segment = "IDX_I" if int(seg_code) == 0 else "NSE_FNO"
                key = (segment, int(sid))
                symbol = self._subscribed.pop(key, None)
                if symbol:
                    symbols.append(symbol)
            if symbols and self.socket and self.connected.is_set():
                self.socket.unsubscribe(symbols=symbols, data_type="SymbolUpdate")
    def close_connection(self):
        self.closed.set()
        if self.socket:
            try:
                self.socket.close_connection()
            except Exception:
                LOG.debug("FYERS websocket close raised", exc_info=True)
