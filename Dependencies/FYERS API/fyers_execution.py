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
        # Defer credentials/network access until the existing live-startup gate
        # explicitly asks this adapter to log in.
        self.app_id = ""
        self.token = ""
        self.api = None
        self.is_logged_in = False
        self.symbols = FyersSymbolMaster()
        self._legacy: dict[str, dict[str, Any]] = {}
        self._poisoned = False

    def _ensure_api(self):
        if self.api is None:
            self.app_id = _env("FYERS_APP_ID")
            self.token = _env("FYERS_ACCESS_TOKEN")
            self.api = fyersModel.FyersModel(
                client_id=self.app_id, token=self.token, is_async=False, log_path=""
            )
        return self.api

    def ensure_logged_in(self):
        resp = self._ensure_api().get_profile()
        if not isinstance(resp, dict) or str(resp.get("s", "")).lower() != "ok":
            raise RuntimeError(f"FYERS profile validation failed: {resp!r}")
        self.is_logged_in = True
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

    def place_market_order(self, symbol: str, side: str, quantity: int,
                           exchange_segment: str = "NSE_FNO", product_type: str = "INTRADAY",
                           *, order_tag: str = "") -> OrderResult:
        if os.getenv("FYERS_LIVE_COMPLIANCE_CONFIRMED", "false").lower() not in ("1", "true", "yes"):
            raise RuntimeError("FYERS live orders blocked: set FYERS_LIVE_COMPLIANCE_CONFIRMED=true only after confirming compliant API app and whitelisted static IP")
        if self._poisoned:
            return normalize_order_result(order_id="", requested_quantity=quantity,
                filled_quantity=None, broker_state="UNKNOWN",
                reason="new orders blocked until explicit reconciliation")
        if isinstance(quantity, bool) or int(quantity) <= 0:
            raise ValueError("quantity must be a positive integer")
        symbol = self._map_symbol(symbol)
        side = 1 if str(side).upper() in ("BUY", "B", "1") else -1
        api = self._ensure_api()
        tag = "".join(c for c in str(order_tag) if c.isalnum())[:20]
        payload = {"symbol": symbol, "qty": int(quantity), "type": 2, "side": side,
                   "productType": "INTRADAY" if str(product_type).upper() in ("INTRADAY", "MIS") else "MARGIN",
                   "limitPrice": 0, "stopPrice": 0, "validity": "DAY",
                   "disclosedQty": 0, "offlineOrder": False, "stopLoss": 0,
                   "takeProfit": 0, "orderTag": tag, "isSliceOrder": False}
        try:
            ack = api.place_order(data=payload)
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
                book = api.orderbook()
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

    @staticmethod
    def _state_label(value: Any) -> str:
        # FYERS order status enum: 1 cancelled, 2 traded, 4 transit,
        # 5 rejected, 6 pending, 7 expired; unknown values stay unknown.
        return {1: "CANCELLED", 2: "TRADED", 4: "TRANSIT", 5: "REJECTED",
                6: "PENDING", 7: "EXPIRED"}.get(value, str(value).upper())

    def get_order_status(self, order_id: str, requested_quantity: int = 0) -> OrderResult:
        api = self._ensure_api()
        book = api.orderbook()
        if not isinstance(book, dict) or str(book.get("s", "")).lower() != "ok":
            return normalize_order_result(order_id=order_id, requested_quantity=max(0, requested_quantity),
                filled_quantity=None, broker_state="UNKNOWN", reason=f"FYERS orderbook response: {book!r}")
        rows = book.get("orderBook", book.get("data", []))
        row = next((r for r in rows if str(r.get("id", r.get("orderNumber", ""))) == str(order_id)), None)
        if not row:
            return normalize_order_result(order_id=order_id, requested_quantity=max(0, requested_quantity),
                filled_quantity=None, broker_state="UNKNOWN", reason="Order not found in FYERS orderbook")
        req = exact_int(row.get("qty", row.get("quantity", requested_quantity)))
        filled = row.get("filledQty", row.get("filled_qty", row.get("tradedQty")))
        state = self._state_label(row.get("status", row.get("orderStatus", "")))
        if req is None:
            req = max(0, requested_quantity)
        return normalize_order_result(order_id=order_id, requested_quantity=req,
            filled_quantity=filled, broker_state=state,
            reason=str(row.get("message", row.get("statusMessage", ""))),
            average_fill_price=row.get("tradedPrice", row.get("avgPrice", 0)))

    def cancel_order(self, order_id: str, requested_quantity: int = 0) -> OrderResult:
        api = self._ensure_api()
        try:
            ack = api.cancel_order(data={"id": str(order_id)})
        except Exception as exc:
            self._poisoned = True
            return normalize_order_result(order_id=order_id, requested_quantity=max(0, requested_quantity),
                filled_quantity=None, broker_state="UNKNOWN", reason=f"cancel outcome ambiguous: {type(exc).__name__}")
        # A cancel acknowledgement is not proof the order is cancelled; inspect orderbook.
        time.sleep(0.25)
        return self.get_order_status(order_id, requested_quantity)

    def list_open_orders(self) -> BrokerQueryResult[OpenOrder]:
        api = self._ensure_api()
        book = api.orderbook()
        if not isinstance(book, dict) or str(book.get("s", "")).lower() != "ok":
            return BrokerQueryResult.indeterminate(f"FYERS orderbook response: {book!r}")
        rows = book.get("orderBook", book.get("data", []))
        open_states = {"PENDING", "OPEN", "TRANSIT", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED"}
        result = []
        for row in rows:
            state = self._state_label(row.get("status", row.get("orderStatus", "")))
            if state not in open_states:
                continue
            req = exact_int(row.get("qty", row.get("quantity")))
            filled = exact_int(row.get("filledQty", row.get("filled_qty", row.get("tradedQty", 0))))
            if req is None or filled is None or req < filled or req < 0 or filled < 0:
                return BrokerQueryResult.indeterminate("Unparseable FYERS open-order quantities")
            result.append(OpenOrder(str(row.get("id", "")), str(row.get("symbol", "")),
                str(row.get("side", "")), req, filled, req-filled, state))
        return BrokerQueryResult.success(result)

    def list_open_positions(self) -> BrokerQueryResult[OpenPosition]:
        resp = self._ensure_api().positions()
        if not isinstance(resp, dict) or str(resp.get("s", "")).lower() != "ok":
            return BrokerQueryResult.indeterminate(f"FYERS positions response: {resp!r}")
        rows = resp.get("netPositions", resp.get("positions", []))
        result = []
        for row in rows:
            qty = exact_int(row.get("netQty", row.get("netqty", row.get("quantity"))))
            if qty is None:
                return BrokerQueryResult.indeterminate("Unparseable FYERS net-position quantity")
            if qty:
                result.append(OpenPosition(str(row.get("symbol", row.get("tradingSymbol", ""))),
                    qty, str(row.get("productType", row.get("product_type", "INTRADAY"))),
                    str(row.get("state", "OPEN"))))
        return BrokerQueryResult.success(result)

    def recover_after_reconciliation(self):
        self._poisoned = False
        return True

    @staticmethod
    def extract_order_id(response):
        if isinstance(response, dict):
            return str(response.get("id", response.get("order_id", "")))
        return ""

    def logout(self) -> dict[str, Any]:
        self.is_logged_in = False
        self.api = None
        return {"status": "success", "message": "FYERS local session cleared"}

fyers_execution_client = FyersExecutionClient()
