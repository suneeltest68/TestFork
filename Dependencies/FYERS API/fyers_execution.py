"""FYERS order adapter implementing the repository's shared ExecutionClient contract.

Live order placement is deliberately gated by the runner's existing global and
per-strategy live flags plus an explicit FYERS compliance acknowledgement.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from fyers_apiv3 import fyersModel

ROOT = Path(__file__).resolve().parents[2]
DEPS = ROOT / "Dependencies"
for entry in (str(ROOT), str(DEPS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
from broker_contract import (  # noqa: E402
    BrokerQueryResult, ExecutionClient, OpenOrder, OpenPosition, OrderResult,
    OrderStatus, exact_int, normalize_order_result,
)
from fyers_common import FyersSymbolMaster  # noqa: E402

LOG = logging.getLogger(__name__)

def _env(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise ValueError(f"{key} must be configured")
    return value

class FyersExecutionClient:
    def __init__(self):
        self.app_id = _env("FYERS_APP_ID")
        self.token = _env("FYERS_ACCESS_TOKEN")
        self.api = fyersModel.FyersModel(client_id=self.app_id, token=self.token, is_async=False, log_path="")
        self.symbols = FyersSymbolMaster()
        self._legacy: dict[str, dict[str, Any]] = {}
        self._poisoned = False

    def ensure_logged_in(self):
        resp = self.api.get_profile()
        if not isinstance(resp, dict) or str(resp.get("s", "")).lower() != "ok":
            raise RuntimeError(f"FYERS profile validation failed: {resp!r}")
        return True

    def preload_scrip_master(self):
        self.symbols.load("NSE_FO", refresh=True)
        self.symbols.load("NSE_CM", refresh=True)
        csv_path = os.getenv("FYERS_LEGACY_INSTRUMENT_CSV", "").strip()
        if csv_path:
            import csv
            with open(csv_path, newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    sid = str(row.get("SECURITY_ID", "")).strip()
                    if not sid:
                        continue
                    self._legacy[sid] = row
        return True

    def resolve_option_symbol(self, underlying: str, expiry, option_type: str, strike: float):
        return self.symbols.option_symbol(underlying, expiry, strike, option_type)

    def _map_symbol(self, symbol: str) -> str:
        value = str(symbol).strip()
        if value.startswith("NSE:"):
            return value
        # Existing workers pass the canonical legacy trading symbol. Resolve it
        # through the preloaded legacy instrument metadata, never by string hacks.
        row = next((r for r in self._legacy.values()
                    if str(r.get("SYMBOL_NAME", "")).strip() == value
                    or str(r.get("DISPLAY_NAME", "")).strip() == value), None)
        if row:
            from datetime import datetime
            expiry_raw = str(row.get("SM_EXPIRY_DATE", ""))[:10]
            expiry = datetime.strptime(expiry_raw, "%Y-%m-%d").date()
            return self.symbols.option_symbol(str(row.get("UNDERLYING_SYMBOL", "NIFTY")),
                expiry, float(row["STRIKE_PRICE"]), str(row["OPTION_TYPE"]))
        raise LookupError(f"No exact FYERS mapping for legacy symbol {value!r}")

    def place_market_order(self, trading_symbol: str, transaction_type: str, quantity: int,
                           product_type: str = "INTRADAY", order_tag: str = "") -> OrderResult:
        if os.getenv("FYERS_LIVE_COMPLIANCE_CONFIRMED", "false").lower() not in ("1", "true", "yes"):
            raise RuntimeError("FYERS live orders blocked: set FYERS_LIVE_COMPLIANCE_CONFIRMED=true only after confirming compliant API app and whitelisted static IP")
        if self._poisoned:
            return normalize_order_result(order_id="", requested_quantity=quantity,
                filled_quantity=None, broker_state="UNKNOWN",
                reason="new orders blocked until explicit reconciliation")
        if isinstance(quantity, bool) or int(quantity) <= 0:
            raise ValueError("quantity must be a positive integer")
        symbol = self._map_symbol(trading_symbol)
        side = 1 if str(transaction_type).upper() in ("BUY", "B") else -1
        tag = "".join(c for c in str(order_tag) if c.isalnum())[:20]
        payload = {"symbol": symbol, "qty": int(quantity), "type": 2, "side": side,
                   "productType": "INTRADAY" if str(product_type).upper() in ("INTRADAY", "MIS") else "MARGIN",
                   "limitPrice": 0, "stopPrice": 0, "validity": "DAY",
                   "disclosedQty": 0, "offlineOrder": False, "stopLoss": 0,
                   "takeProfit": 0, "orderTag": tag, "isSliceOrder": False}
        try:
            ack = self.api.place_order(data=payload)
        except Exception as exc:
            self._poisoned = True
            return normalize_order_result(order_id="", requested_quantity=int(quantity),
                filled_quantity=None, broker_state="UNKNOWN",
                reason=f"FYERS order transport outcome ambiguous: {type(exc).__name__}")
        if not isinstance(ack, dict) or str(ack.get("s", "")).lower() != "ok" or not ack.get("id"):
            # An error envelope alone is not treated as proof of zero fill.
            self._poisoned = True
            return normalize_order_result(order_id="", requested_quantity=int(quantity),
                filled_quantity=None, broker_state="UNKNOWN", reason=f"ambiguous FYERS response: {ack!r}")
        order_id = str(ack["id"])
        deadline = time.monotonic() + 10.0
        last = None
        while time.monotonic() < deadline:
            try:
                book = self.api.orderbook()
                if isinstance(book, dict) and str(book.get("s", "")).lower() == "ok":
                    rows = book.get("orderBook", book.get("data", []))
                    last = next((r for r in rows if str(r.get("id", r.get("orderNumber", ""))) == order_id), None)
                    if last:
                        state = str(last.get("status", last.get("orderStatus", "")))
                        filled = last.get("filledQty", last.get("filled_qty", last.get("tradedQty")))
                        result = normalize_order_result(order_id=order_id, requested_quantity=int(quantity),
                            filled_quantity=filled, broker_state=state,
                            reason=str(last.get("message", last.get("statusMessage", ""))),
                            average_fill_price=last.get("tradedPrice", last.get("avgPrice", 0)))
                        if result.status is not OrderStatus.UNKNOWN:
                            return result
            except Exception:
                LOG.warning("FYERS order confirmation poll failed", exc_info=True)
            time.sleep(0.4)
        self._poisoned = True
        return normalize_order_result(order_id=order_id, requested_quantity=int(quantity),
            filled_quantity=None, broker_state=str((last or {}).get("status", "TIMEOUT")),
            reason="FYERS order not conclusively confirmed; reconcile before any new order")

    def get_order_status(self, order_id: str):
        book = self.api.orderbook()
        if not isinstance(book, dict) or str(book.get("s", "")).lower() != "ok":
            return BrokerQueryResult(ok=False, error=f"FYERS orderbook response: {book!r}")
        rows = book.get("orderBook", book.get("data", []))
        row = next((r for r in rows if str(r.get("id", "")) == str(order_id)), None)
        return BrokerQueryResult(ok=True, data=row)

    def cancel_order(self, order_id: str):
        return self.api.cancel_order(data={"id": str(order_id)})

    def list_open_orders(self):
        book = self.api.orderbook()
        if not isinstance(book, dict) or str(book.get("s", "")).lower() != "ok":
            return BrokerQueryResult(ok=False, error=f"FYERS orderbook response: {book!r}")
        rows = book.get("orderBook", book.get("data", []))
        open_states = {"PENDING", "OPEN", "TRANSIT", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED"}
        return BrokerQueryResult(ok=True, data=[r for r in rows if str(r.get("status", "")).upper() in open_states])

    def list_open_positions(self):
        resp = self.api.positions()
        if not isinstance(resp, dict) or str(resp.get("s", "")).lower() != "ok":
            return BrokerQueryResult(ok=False, error=f"FYERS positions response: {resp!r}")
        rows = resp.get("netPositions", resp.get("positions", []))
        return BrokerQueryResult(ok=True, data=rows)

    def recover_after_reconciliation(self):
        self._poisoned = False
        return True

    @staticmethod
    def extract_order_id(response):
        if isinstance(response, dict):
            return str(response.get("id", response.get("order_id", "")))
        return ""

    def logout(self):
        return None

fyers_execution_client = FyersExecutionClient
