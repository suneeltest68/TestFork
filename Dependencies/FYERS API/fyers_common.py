"""FYERS symbol-master and legacy-instrument mapping helpers.

No credentials or network calls happen at import time. FYERS symbols are always
taken from the broker's published symbol masters; option symbols are never
constructed by guessing expiry formatting.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

LOG = logging.getLogger(__name__)
MASTER_URLS = {
    "NSE_FO": "https://public.fyers.in/sym_details/NSE_FO_sym_master.json",
    "NSE_CM": "https://public.fyers.in/sym_details/NSE_CM_sym_master.json",
}
MASTER_CACHE = Path(os.getenv("FYERS_SYMBOL_CACHE", "Dependencies/fyers_symbol_cache"))


def legacy_instrument_csv() -> Path | None:
    configured = os.getenv("FYERS_LEGACY_INSTRUMENT_CSV", "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
    root = Path(__file__).resolve().parents[1]
    candidates = sorted(root.glob("all_instrument*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


INDEX_ALIASES = {
    "13": ("NIFTY", "NSE:NIFTY50-INDEX"),
    "25": ("BANKNIFTY", "NSE:NIFTYBANK-INDEX"),
    "NIFTY": ("NIFTY", "NSE:NIFTY50-INDEX"),
    "BANKNIFTY": ("BANKNIFTY", "NSE:NIFTYBANK-INDEX"),
    "FINNIFTY": ("FINNIFTY", "NSE:FINNIFTY-INDEX"),
}


def _first(record: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if record.get(name) not in (None, ""):
            return record[name]
    return default


def _expiry(record: dict[str, Any]) -> date | None:
    raw = _first(record, "expiryDate", "expiry_date", "expiry")
    if raw is None:
        return None
    try:
        number = float(raw)
        if number > 1_000_000_000:
            return datetime.fromtimestamp(number, timezone.utc).astimezone(ZoneInfo('Asia/Kolkata')).date()
    except (TypeError, ValueError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(str(raw)[:10], fmt).date()
        except ValueError:
            continue
    return None


class FyersSymbolMaster:
    """Cached, exact-symbol lookups against FYERS public daily symbol masters."""
    def __init__(self, cache_dir: Path | str = MASTER_CACHE, timeout: float = 12.0):
        self.cache_dir = Path(cache_dir)
        self.timeout = float(timeout)
        self._records: dict[str, list[dict[str, Any]]] = {}

    def load(self, segment: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        if segment not in MASTER_URLS:
            raise ValueError(f"Unsupported FYERS symbol-master segment: {segment}")
        if segment in self._records and not refresh:
            return self._records[segment]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{segment}.json"
        payload: Any = None
        if not refresh and path.is_file() and time.time() - path.stat().st_mtime < 86400:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
        if payload is None:
            response = requests.get(MASTER_URLS[segment], timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, (list, dict)):
                raise ValueError(f"Unexpected FYERS {segment} symbol master payload")
            path.write_text(json.dumps(payload), encoding="utf-8")
        if isinstance(payload, dict):
            records = payload.get("data", payload.get("symbols"))
            if records is None:
                # The published JSON masters may be keyed by the exact ticker
                # rather than wrapped in a top-level list.
                records = [
                    ({"symTicker": key, **value} if isinstance(value, dict) else None)
                    for key, value in payload.items()
                ]
        else:
            records = payload
        if not isinstance(records, list):
            raise ValueError(f"FYERS {segment} symbol master has an unsupported shape")
        self._records[segment] = [r for r in records if isinstance(r, dict)]
        return self._records[segment]

    @staticmethod
    def symbol(record: dict[str, Any]) -> str:
        value = _first(record, "symTicker", "symbol", "trading_symbol", "ticker")
        return str(value).strip() if value else ""

    def index_symbol(self, alias: str) -> str:
        name = INDEX_ALIASES.get(str(alias).upper(), (str(alias).upper(), ""))[0]
        candidates = self.load("NSE_CM")
        # Prefer the exact well-known index ticker if it exists in the current master.
        exact = [self.symbol(r) for r in candidates if self.symbol(r).upper() in {
            f"NSE:{name}-INDEX", f"{name}-INDEX"
        }]
        if exact:
            return exact[0] if exact[0].startswith("NSE:") else f"NSE:{exact[0]}"
        fallback = INDEX_ALIASES.get(str(alias).upper(), ("", ""))[1]
        if any(self.symbol(r) == fallback for r in candidates):
            return fallback
        raise LookupError(f"FYERS index symbol for {alias!r} not found in NSE_CM master")

    def option_symbol(self, underlying: str, expiry: date, strike: float, right: str) -> str:
        root = str(underlying).upper().replace("NSE:", "")
        right_code = {"CE": "CE", "PE": "PE", "C": "CE", "P": "PE"}.get(str(right).upper())
        if not right_code:
            raise ValueError(f"Unsupported option right: {right}")
        matches: list[str] = []
        for record in self.load("NSE_FO"):
            symbol = self.symbol(record)
            if not symbol:
                continue
            ex_name = str(_first(record, "exSymName", "underlying", "underlying_symbol", default="")).upper()
            details = str(_first(record, "symDetails", "description", "displayName", default="")).upper()
            ticker = symbol.upper()
            record_root = ex_name or details or ticker
            if root not in record_root and not ticker.startswith(f"NSE:{root}"):
                continue
            exp = _expiry(record)
            if exp != expiry:
                continue
            raw_strike = _first(record, "strikePrice", "strike_price", "strike")
            try:
                if abs(float(raw_strike) - float(strike)) > 0.001:
                    continue
            except (TypeError, ValueError):
                continue
            opt = str(_first(record, "optType", "option_type", "optionType", default="")).upper()
            if opt not in (right_code, "CALL" if right_code == "CE" else "PUT"):
                if not ticker.endswith(right_code):
                    continue
            matches.append(symbol if symbol.startswith("NSE:") else f"NSE:{symbol}")
        if len(matches) != 1:
            raise LookupError(
                f"Expected one FYERS symbol for {root}/{expiry}/{strike}/{right_code}; "
                f"found {len(matches)}. Refresh symbol master or verify contract metadata."
            )
        return matches[0]
