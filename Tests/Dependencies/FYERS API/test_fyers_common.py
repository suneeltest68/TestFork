from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FYERS_DIR = ROOT / "Dependencies" / "FYERS API"
sys.path.insert(0, str(FYERS_DIR))
sys.path.insert(0, str(ROOT))

from fyers_common import FyersSymbolMaster  # noqa: E402



class StaticMaster(FyersSymbolMaster):
    def __init__(self, records):
        super().__init__()
        self.records = records

    def load(self, segment: str, *, refresh: bool = False):
        return self.records[segment]



def test_option_lookup_uses_exact_published_symbol():
    master = StaticMaster({
        "NSE_FO": [{
            "symTicker": "NSE:NIFTY26OCT26000CE",
            "exSymName": "NIFTY",
            "expiryDate": "2026-10-27",
            "strikePrice": 26000,
            "optType": "CE",
        }],
        "NSE_CM": [],
    })
    assert master.option_symbol("NIFTY", date(2026, 10, 27), 26000, "CE") == "NSE:NIFTY26OCT26000CE"



def test_option_lookup_fails_closed_when_symbol_is_ambiguous():
    record = {
        "symTicker": "NSE:NIFTY26OCT26000CE",
        "exSymName": "NIFTY",
        "expiryDate": "2026-10-27",
        "strikePrice": 26000,
        "optType": "CE",
    }
    master = StaticMaster({"NSE_FO": [record, dict(record)], "NSE_CM": []})
    try:
        master.option_symbol("NIFTY", date(2026, 10, 27), 26000, "CE")
    except LookupError as exc:
        assert "found 2" in str(exc)
    else:
        raise AssertionError("ambiguous contract must never be selected")
