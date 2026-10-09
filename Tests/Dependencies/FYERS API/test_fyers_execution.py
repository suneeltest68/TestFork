from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
FYERS_DIR = ROOT / "Dependencies" / "FYERS API"
sys.path.insert(0, str(FYERS_DIR))
sys.path.insert(0, str(ROOT))

import os

import pytest

from fyers_execution import FyersExecutionClient


def test_live_orders_fail_closed_without_compliance_ack(monkeypatch):
    monkeypatch.delenv("FYERS_LIVE_COMPLIANCE_CONFIRMED", raising=False)
    client = FyersExecutionClient()
    with pytest.raises(RuntimeError, match="live orders blocked"):
        client.place_market_order("NSE:NIFTY26OCT26000CE", "BUY", 25)


def test_order_result_requires_confirmed_full_fill(monkeypatch):
    monkeypatch.setenv("FYERS_LIVE_COMPLIANCE_CONFIRMED", "true")
    client = FyersExecutionClient()
    client.api = FakeFyersApi()
    client.app_id = "test-app"
    client.token = "test-token"
    result = client.place_market_order("NSE:NIFTY26OCT26000CE", "BUY", 25, order_tag="unit-test")
    assert result.status.value == "FILLED"
    assert result.filled_quantity == 25
    assert result.average_fill_price == 102.5


class FakeFyersApi:
    def place_order(self, data):
        assert data["symbol"] == "NSE:NIFTY26OCT26000CE"
        assert data["side"] == 1
        return {"s": "ok", "id": "TEST-ORDER-1"}

    def orderbook(self):
        return {"s": "ok", "orderBook": [{
            "id": "TEST-ORDER-1", "status": 2, "qty": 25,
            "filledQty": 25, "tradedPrice": 102.5,
        }]}
