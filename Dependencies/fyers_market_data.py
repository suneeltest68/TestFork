"""Fyers REST and WebSocket market-data adapter for the trading runner."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

FYERS_NSE_FO_SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
FYERS_NIFTY_SYMBOL = "NSE:NIFTY50-INDEX"
FYERS_BANKNIFTY_SYMBOL = "NSE:NIFTYBANK-INDEX"
FYERS_FINNIFTY_SYMBOL = "NSE:FINNIFTY-INDEX"
FYERS_EXPIRED_FNO_API_URL = "https://api-t1.fyers.in/data/history/fno/expired"
FYERS_INDEX_SYMBOLS = {
    13: FYERS_NIFTY_SYMBOL,
    25: FYERS_BANKNIFTY_SYMBOL,
    27: FYERS_FINNIFTY_SYMBOL,
}
RUNNER_SEGMENT_CODES = {"IDX_I": 0, "NSE_FNO": 2}
FYERS_QUOTE_BATCH_SIZE = 50
FYERS_SYMBOL_MASTER_TIMEOUT_SECONDS = 30
FYERS_HISTORY_RESOLUTIONS = frozenset({1, 2, 3, 5, 10, 15, 20, 30, 60, 120, 240, "5S"})
FYERS_EXPIRED_FNO_RESOLUTIONS = frozenset(
    {"5S", "1", "2", "3", "5", "10", "15", "20", "30", "45", "60", "120", "180", "240", "1D", "1W", "1M"}
)
FYERS_MIN_HISTORY_REQUEST_INTERVAL_SECONDS = 0.6
FYERS_HISTORY_BAD_REQUEST_RETRIES = 2
_IST = ZoneInfo("Asia/Kolkata")
_LOGGER = logging.getLogger(__name__)


def _contract_key(underlying: object, expiry: object, strike: object, option_type: object):
    """Build a stable key shared by Fyers and the existing Dhan contract master."""
    try:
        expiry_date = pd.to_datetime(expiry, errors="raise").date()
        strike_value = float(strike)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(strike_value):
        return None
    right = str(option_type).strip().upper()
    if right not in {"CE", "PE"}:
        return None
    return (
        str(underlying).strip().upper(),
        expiry_date,
        round(strike_value, 4),
        right,
    )


def _fyers_expiry_date(value: object) -> date | None:
    """Convert the Fyers master expiry epoch (seconds) into an IST calendar date."""
    try:
        return datetime.fromtimestamp(float(value), tz=_IST).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


class FyersMarketDataClient:
    """Adapt Fyers market-data calls to the runner's existing feed contract.

    The runner's existing contract-selection source is the public Dhan detailed
    instrument master; the adapter downloads it if the local copy is absent and
    requires no Dhan login. Fyers' public NSE F&O symbol master is joined by
    underlying, expiry, strike and right to translate selected contracts into
    symbols accepted by Fyers data APIs.
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        instrument_master_glob: str,
        fyers_symbol_master_path: Path,
        *,
        model: Any | None = None,
        symbol_master_frame: pd.DataFrame | None = None,
        dhan_master_frame: pd.DataFrame | None = None,
        request_timeout_seconds: float = 10.0,
        load_symbol_mappings: bool = True,
    ) -> None:
        self.client_id = str(client_id).strip()
        self.access_token = str(access_token).strip()
        if not self.client_id or not self.access_token:
            raise ValueError("FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN must be configured.")
        if not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("Fyers HTTP timeout must be a finite, positive number.")
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.instrument_master_glob = str(instrument_master_glob)
        self.fyers_symbol_master_path = Path(fyers_symbol_master_path)
        self._model = model or self._create_model()
        self._install_request_timeout()
        self._symbol_by_dhan_id: dict[int, str] = dict(FYERS_INDEX_SYMBOLS)
        self._identity_by_symbol: dict[str, tuple[str, int]] = {
            symbol: ("IDX_I", security_id) for security_id, symbol in FYERS_INDEX_SYMBOLS.items()
        }
        self._expiry_timestamp_by_date: dict[date, int] = {}
        self._last_history_request_at = 0.0
        if load_symbol_mappings:
            self._load_symbol_mappings(symbol_master_frame, dhan_master_frame)

    def _install_request_timeout(self) -> None:
        """Bound every synchronous SDK HTTP request, including redirects."""
        service = getattr(self._model, "service", None)
        session = getattr(service, "session", None)
        original_request = getattr(session, "request", None)
        if not callable(original_request):
            return

        timeout = self.request_timeout_seconds

        def request_with_timeout(method: str, url: str, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", timeout)
            return original_request(method, url, **kwargs)

        session.request = request_with_timeout

    def _create_model(self) -> Any:
        """Build the official Fyers v3 REST client, reporting a clear missing-SDK error."""
        try:
            from fyers_apiv3 import fyersModel
        except ImportError as exc:
            raise RuntimeError(
                "Fyers API client is missing. Install the project dependencies from requirements.txt."
            ) from exc
        return fyersModel.FyersModel(
            client_id=self.client_id,
            token=self.access_token,
            is_async=False,
            log_path="",
        )

    def validate_session(self) -> None:
        """Fail early if the configured Fyers access token is invalid or expired."""
        response = self._model.get_profile()
        if not isinstance(response, dict) or str(response.get("s", "")).lower() != "ok":
            detail = (
                response.get("message", "invalid response") if isinstance(response, dict) else type(response).__name__
            )
            raise RuntimeError(f"Fyers access-token validation failed: {detail}")

    def _read_fyers_symbol_master(self) -> pd.DataFrame:
        """Refresh the public Fyers NSE F&O symbol master atomically."""
        response = requests.get(
            FYERS_NSE_FO_SYMBOL_MASTER_URL,
            timeout=FYERS_SYMBOL_MASTER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        self.fyers_symbol_master_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.fyers_symbol_master_path.with_suffix(".csv.tmp")
        temporary.write_bytes(response.content)
        temporary.replace(self.fyers_symbol_master_path)
        return pd.read_csv(self.fyers_symbol_master_path, header=None, dtype=str, low_memory=False)

    def _read_dhan_instrument_master(self) -> pd.DataFrame:
        """Read the runner contract master, downloading the public file if absent."""
        import glob

        matches = glob.glob(self.instrument_master_glob)
        if not matches:
            master_path = Path(self.instrument_master_glob).parent / (
                f"all_instrument {datetime.now(_IST).date().isoformat()}.csv"
            )
            master_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = master_path.with_suffix(".csv.part")
            response = requests.get(
                DHAN_SCRIP_MASTER_URL,
                timeout=(15.0, 120.0),
                stream=True,
            )
            try:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            output.write(chunk)
                columns = set(pd.read_csv(temporary, nrows=0).columns)
                required = {
                    "SECURITY_ID",
                    "UNDERLYING_SYMBOL",
                    "SM_EXPIRY_DATE",
                    "STRIKE_PRICE",
                    "OPTION_TYPE",
                }
                if not required.issubset(columns):
                    missing = sorted(required - columns)
                    raise ValueError(f"Downloaded contract master is missing required columns: {missing}")
                temporary.replace(master_path)
            finally:
                response.close()
                temporary.unlink(missing_ok=True)
            matches = [str(master_path)]
        matches.sort(key=lambda path: Path(path).stat().st_mtime, reverse=True)
        return pd.read_csv(matches[0], dtype=str, low_memory=False)

    def _load_symbol_mappings(
        self,
        symbol_master_frame: pd.DataFrame | None,
        dhan_master_frame: pd.DataFrame | None,
    ) -> None:
        """Join provider symbol masters and keep both directions for data calls."""
        fyers_master = symbol_master_frame
        if fyers_master is None:
            fyers_master = self._read_fyers_symbol_master()
        dhan_master = dhan_master_frame
        if dhan_master is None:
            dhan_master = self._read_dhan_instrument_master()
        if fyers_master.shape[1] < 17:
            raise ValueError("Fyers NSE F&O symbol master has an unexpected column layout.")

        fyers_by_identity: dict[tuple[str, date, float, str], tuple[str, int]] = {}
        for row in fyers_master.itertuples(index=False, name=None):
            if len(row) < 17:
                continue
            expiry_date = _fyers_expiry_date(row[8])
            strike = _contract_key(row[13], expiry_date, row[15], row[16])
            if strike is None:
                continue
            key = strike
            try:
                expiry_timestamp = int(float(row[8]))
            except (TypeError, ValueError, OverflowError):
                continue
            symbol = str(row[9]).strip()
            if not symbol.startswith("NSE:"):
                continue
            fyers_by_identity[key] = (symbol, expiry_timestamp)

        required_columns = {
            "SECURITY_ID",
            "UNDERLYING_SYMBOL",
            "SM_EXPIRY_DATE",
            "STRIKE_PRICE",
            "OPTION_TYPE",
        }
        if not required_columns.issubset(dhan_master.columns):
            missing = sorted(required_columns - set(dhan_master.columns))
            raise ValueError(f"Dhan contract master is missing columns: {missing}")

        mapped = 0
        for row in dhan_master.to_dict(orient="records"):
            key = _contract_key(
                row.get("UNDERLYING_SYMBOL"),
                row.get("SM_EXPIRY_DATE"),
                row.get("STRIKE_PRICE"),
                row.get("OPTION_TYPE"),
            )
            if key is None or key not in fyers_by_identity:
                continue
            try:
                security_id = int(float(row["SECURITY_ID"]))
            except (TypeError, ValueError, OverflowError):
                continue
            symbol, expiry_timestamp = fyers_by_identity[key]
            self._symbol_by_dhan_id[security_id] = symbol
            self._identity_by_symbol[symbol] = ("NSE_FNO", security_id)
            self._expiry_timestamp_by_date[key[1]] = expiry_timestamp
            mapped += 1
        if mapped == 0:
            raise ValueError("Fyers and Dhan contract masters did not contain any matching option contracts.")
        _LOGGER.info("Loaded %s Fyers option symbols from the current contract masters.", mapped)

    def _symbol_for_security(self, security_id: int) -> str:
        try:
            return self._symbol_by_dhan_id[int(security_id)]
        except KeyError as exc:
            raise KeyError(f"No Fyers symbol is mapped for Dhan security ID {security_id}.") from exc

    def _symbol_for_identity(self, exchange_segment: str, security_id: int) -> str:
        """Resolve a runner segment/id pair without accepting mismatched identities."""
        symbol = self._symbol_for_security(security_id)
        if self._identity_by_symbol.get(symbol) != (str(exchange_segment), int(security_id)):
            raise KeyError(f"No Fyers symbol is mapped for {exchange_segment} security ID {security_id}.")
        return symbol

    def fetch_index_1m_ohlc(
        self,
        security_id: int,
        exchange_segment: str,
        instrument_type: str,
        lookback_days: int = 7,
    ) -> pd.DataFrame:
        """Fetch recent one-minute candles and normalize them to runner timestamps."""
        if str(exchange_segment) != "IDX_I" or str(instrument_type).upper() != "INDEX":
            raise ValueError("Fyers index candles require exchange_segment='IDX_I' and instrument_type='INDEX'.")
        end_date = datetime.now(_IST).date()
        start_date = end_date - timedelta(days=max(1, int(lookback_days)))
        frame = self.fetch_index_history(
            security_id,
            start_date,
            end_date,
            interval=1,
        )
        if not frame.empty:
            frame["timestamp"] = (
                pd.to_datetime(frame["timestamp"], unit="s", utc=True).dt.tz_convert(_IST).dt.tz_localize(None)
            )
        return frame[["timestamp", "open", "high", "low", "close", "volume"]]

    def fetch_index_history(
        self,
        security_id: int,
        start_date: date,
        end_date: date,
        *,
        interval: int,
    ) -> pd.DataFrame:
        """Fetch one Fyers history window at the requested minute resolution."""
        try:
            symbol = FYERS_INDEX_SYMBOLS[int(security_id)]
        except KeyError as exc:
            raise ValueError(f"Unsupported Fyers index security ID: {security_id}") from exc
        return self.fetch_history(
            symbol,
            start_date,
            end_date,
            resolution=interval,
        )

    def fetch_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        *,
        resolution: int | str,
    ) -> pd.DataFrame:
        """Fetch candles for a Fyers symbol at a supported minute or 5-second resolution."""
        if start_date > end_date:
            raise ValueError("Fyers history start date must not be after its end date.")
        normalized_resolution: int | str = resolution.upper() if isinstance(resolution, str) else resolution
        if normalized_resolution not in FYERS_HISTORY_RESOLUTIONS:
            raise ValueError(f"Unsupported Fyers history resolution: {resolution}")
        request = {
            "symbol": symbol,
            "resolution": str(normalized_resolution),
            "date_format": "1",
            "range_from": start_date.isoformat(),
            "range_to": end_date.isoformat(),
            "cont_flag": "1",
        }
        for attempt in range(FYERS_HISTORY_BAD_REQUEST_RETRIES + 1):
            self._wait_for_history_request_slot()
            response = self._model.history(data=request)
            self._last_history_request_at = time.monotonic()
            if (
                isinstance(response, dict)
                and str(response.get("s", "")).lower() == "error"
                and str(response.get("message", "")).strip().lower() == "bad request"
                and attempt < FYERS_HISTORY_BAD_REQUEST_RETRIES
            ):
                _LOGGER.warning(
                    "Fyers history returned a transient bad request for %s; retrying (%s/%s).",
                    symbol,
                    attempt + 1,
                    FYERS_HISTORY_BAD_REQUEST_RETRIES,
                )
                time.sleep(attempt + 1)
                continue
            break
        if not isinstance(response, dict):
            raise RuntimeError(f"Fyers history request failed for {symbol}: invalid response")
        status = str(response.get("s", "")).lower()
        if status in {"no_data", "error"} and any(
            marker in str(response.get("message", "")).lower() for marker in ("no data", "no records")
        ):
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        if status != "ok":
            detail = (
                response.get("message", "invalid response") if isinstance(response, dict) else type(response).__name__
            )
            raise RuntimeError(f"Fyers history request failed for {symbol}: {detail}")
        candles = response.get("candles")
        if not isinstance(candles, list):
            raise ValueError(f"Fyers history response for {symbol} has no candle list.")
        if not candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
        frame["timestamp"] = frame.pop("epoch")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        return frame[["timestamp", "open", "high", "low", "close", "volume"]]

    def fetch_expiry_dates(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        """Fetch expired-options expiry dates available for an underlying."""
        response = self._fetch_expired_fno_endpoint(
            "expiry-dates",
            {
                "symbol": symbol,
                "range_from": start_date.isoformat(),
                "range_to": end_date.isoformat(),
                "date_format": "1",
            },
        )
        data = response.get("data")
        expiry_dates = data.get("expiry_dates") if isinstance(data, dict) else None
        options = expiry_dates.get("options") if isinstance(expiry_dates, dict) else None
        if not isinstance(options, list):
            raise ValueError("Fyers expiry-dates response has no options expiry list.")
        try:
            return sorted({date.fromisoformat(str(value)) for value in options})
        except ValueError as exc:
            raise ValueError("Fyers expiry-dates response contains an invalid date.") from exc

    def fetch_expired_option_symbols(
        self,
        symbol: str,
        expiry_date: date,
    ) -> list[str]:
        """Fetch expired option symbols for an underlying and expiry."""
        response = self._fetch_expired_fno_endpoint(
            "underlying-symbols",
            {"symbol": symbol, "expiry_date": expiry_date.isoformat()},
        )
        data = response.get("data")
        contracts = data.get("contracts") if isinstance(data, dict) else None
        options = contracts.get("options") if isinstance(contracts, dict) else None
        if not isinstance(options, list) or any(not isinstance(item, str) for item in options):
            raise ValueError("Fyers expired-contracts response has no valid options list.")
        return options

    def fetch_expired_option_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        *,
        resolution: int | str,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles for an expired option contract."""
        normalized_resolution = str(resolution).upper()
        if normalized_resolution not in FYERS_EXPIRED_FNO_RESOLUTIONS:
            raise ValueError(f"Unsupported expired F&O history resolution: {resolution}")
        if start_date > end_date:
            raise ValueError("Expired F&O history start date must not be after its end date.")
        response = self._fetch_expired_fno_endpoint(
            "historical-data",
            {
                "symbol": symbol,
                "resolution": str(normalized_resolution),
                "date_format": "1",
                "range_from": start_date.isoformat(),
                "range_to": end_date.isoformat(),
            },
            allow_no_data=True,
        )
        candles = response.get("candles")
        if not isinstance(candles, list):
            raise ValueError(f"Fyers expired history response for {symbol} has no candle list.")
        if not candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        columns = response.get("columns")
        if not isinstance(columns, list) or not {"timestamp", "open", "high", "low", "close", "volume"}.issubset(
            columns
        ):
            raise ValueError(f"Fyers expired history response for {symbol} has invalid candle columns.")
        frame = pd.DataFrame(candles, columns=columns)
        for column in ("timestamp", "open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        return frame[["timestamp", "open", "high", "low", "close", "volume"]]

    def _fetch_expired_fno_endpoint(
        self,
        endpoint: str,
        params: dict[str, str],
        *,
        allow_no_data: bool = False,
    ) -> dict[str, object]:
        """Call one documented Fyers expired-F&O endpoint with the SDK credentials."""
        self._wait_for_history_request_slot()
        response = requests.get(
            f"{FYERS_EXPIRED_FNO_API_URL}/{endpoint}",
            params=params,
            headers={
                "Authorization": f"{self.client_id}:{self.access_token}",
                "Content-Type": "application/json",
                "version": "3",
            },
            timeout=self.request_timeout_seconds,
        )
        self._last_history_request_at = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Fyers expired F&O {endpoint} endpoint returned an invalid response.")
        status = str(payload.get("s", "")).lower()
        if allow_no_data and status == "no_data":
            return payload
        if status != "ok":
            detail = payload.get("message", "invalid response")
            code = payload.get("code", "unknown")
            raise RuntimeError(f"Fyers expired F&O {endpoint} request failed ({code}): {detail}")
        return payload

    def _wait_for_history_request_slot(self) -> None:
        """Keep all Fyers history endpoints below the observed request burst limit."""
        elapsed = time.monotonic() - self._last_history_request_at
        if elapsed < FYERS_MIN_HISTORY_REQUEST_INTERVAL_SECONDS:
            time.sleep(FYERS_MIN_HISTORY_REQUEST_INTERVAL_SECONDS - elapsed)

    def fetch_ltp_map(self, securities_by_segment: dict[str, list[int]]) -> dict[tuple[str, int], float]:
        """Fetch quotes in Fyers-sized batches and key them by runner segment/id."""
        requested: dict[str, tuple[str, int]] = {}
        for segment, security_ids in (securities_by_segment or {}).items():
            for security_id in security_ids:
                sid = int(security_id)
                normalized_segment = str(segment)
                symbol = self._symbol_for_identity(normalized_segment, sid)
                requested[symbol] = (normalized_segment, sid)
        result: dict[tuple[str, int], float] = {}
        symbols = list(requested)
        for start in range(0, len(symbols), FYERS_QUOTE_BATCH_SIZE):
            batch = symbols[start : start + FYERS_QUOTE_BATCH_SIZE]
            response = self._model.quotes(data={"symbols": ",".join(batch)})
            if not isinstance(response, dict) or str(response.get("s", "")).lower() != "ok":
                detail = (
                    response.get("message", "invalid response")
                    if isinstance(response, dict)
                    else type(response).__name__
                )
                raise RuntimeError(f"Fyers quote request failed: {detail}")
            entries = response.get("d")
            if not isinstance(entries, list):
                raise ValueError("Fyers quote response has no 'd' list.")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                symbol = str(entry.get("n", "")).strip()
                values = entry.get("v")
                if symbol not in requested or not isinstance(values, dict):
                    continue
                try:
                    price = float(values.get("lp", 0))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(price) and price > 0:
                    result[requested[symbol]] = price
        return result

    def fetch_option_chain(
        self,
        under_security_id: int,
        under_exchange_segment: str,
        expiry: date,
    ) -> dict[str, Any]:
        """Translate the Fyers option-chain response to the runner's chain shape."""
        if str(under_exchange_segment) != "IDX_I":
            raise ValueError("Fyers option chains require an IDX_I underlying.")
        symbol = FYERS_INDEX_SYMBOLS.get(int(under_security_id))
        if symbol is None:
            raise ValueError(f"Unsupported Fyers underlying security ID: {under_security_id}")
        expiry_timestamp = self._expiry_timestamp_by_date.get(expiry)
        if expiry_timestamp is None:
            raise ValueError(f"Fyers contract master has no expiry timestamp for {expiry}.")
        response = self._model.optionchain(
            data={
                "symbol": symbol,
                "strikecount": 50,
                "timestamp": str(expiry_timestamp),
                "greeks": "1",
            }
        )
        if not isinstance(response, dict) or str(response.get("s", "")).lower() != "ok":
            detail = (
                response.get("message", "invalid response") if isinstance(response, dict) else type(response).__name__
            )
            raise RuntimeError(f"Fyers option-chain request failed for {expiry}: {detail}")
        payload = response.get("data")
        if not isinstance(payload, dict) or not isinstance(payload.get("optionsChain"), list):
            raise ValueError("Fyers option-chain response has no optionsChain list.")
        chain: dict[str, dict[str, Any]] = {}
        for entry in payload["optionsChain"]:
            if not isinstance(entry, dict):
                continue
            try:
                strike = float(entry.get("strike_price"))
                ltp = float(entry.get("ltp", 0))
            except (TypeError, ValueError):
                continue
            right = str(entry.get("option_type", "")).strip().lower()
            if right not in {"ce", "pe"}:
                continue
            option_greeks = entry.get("option_greeks")
            if not isinstance(option_greeks, dict):
                option_greeks = {}
            node = {
                "last_price": ltp,
                "open_interest": entry.get("oi", 0),
                "implied_volatility": option_greeks.get("iv", entry.get("iv", 0)),
                "top_bid_price": entry.get("bid", 0),
                "top_ask_price": entry.get("ask", 0),
                "greeks": {
                    name: option_greeks.get(name, entry.get(name, 0)) for name in ("delta", "gamma", "theta", "vega")
                },
            }
            chain.setdefault(f"{strike:.4f}", {})[right] = node
        return {
            "status": "success",
            "data": {
                "last_price": payload.get("underlyingValue", 0),
                "oc": chain,
            },
        }

    def make_market_feed(self, instruments: list[tuple[str, str]]) -> FyersMarketFeed:
        """Create an event-queue wrapper for the Fyers websocket data socket."""
        return FyersMarketFeed(self, instruments)


class FyersMarketFeed:
    """Queue-backed adapter around Fyers' callback-based data socket."""

    _CLOSED = object()

    def __init__(self, client: FyersMarketDataClient, instruments: list[tuple[str, str]]) -> None:
        try:
            from fyers_apiv3.FyersWebsocket import data_ws
        except ImportError as exc:
            raise RuntimeError("The Fyers WebSocket client is not installed.") from exc
        self._client = client
        self._queue: queue.Queue[object] = queue.Queue()
        self._connected = threading.Event()
        self._error: object | None = None
        self._initial_symbols = [client._symbol_for_identity(str(item[0]), int(item[1])) for item in instruments]

        def on_message(message: object) -> None:
            self._queue.put(message)

        def on_error(message: object) -> None:
            self._error = message
            self._queue.put(self._CLOSED)

        def on_close(message: object) -> None:
            self._error = message
            self._queue.put(self._CLOSED)

        def on_connect() -> None:
            self._socket.subscribe(symbols=self._initial_symbols, data_type="SymbolUpdate")
            self._connected.set()
            self._socket.keep_running()

        self._socket = data_ws.FyersDataSocket(
            access_token=f"{client.client_id}:{client.access_token}",
            log_path="",
            litemode=False,
            write_to_file=False,
            reconnect=False,
            on_connect=on_connect,
            on_close=on_close,
            on_error=on_error,
            on_message=on_message,
        )
        self._connection_thread: threading.Thread | None = None

    def run_forever(self) -> None:
        """Start the SDK connection on its own thread and wait for a handshake."""
        self._connection_thread = threading.Thread(
            target=self._socket.connect,
            name="FyersDataSocket",
            daemon=True,
        )
        self._connection_thread.start()
        if not self._connected.wait(timeout=10):
            if self._error is not None:
                raise RuntimeError(f"Fyers websocket connection failed: {self._error}")
            raise TimeoutError("Fyers websocket did not complete its connection handshake.")

    def get_data(self) -> object:
        """Return a normalized Dhan-shaped tick so existing pure parsers remain reusable."""
        try:
            packet = self._queue.get(timeout=0.5)
        except queue.Empty:
            return None
        if packet is self._CLOSED:
            raise RuntimeError(f"Fyers websocket closed or failed: {self._error}")
        if not isinstance(packet, dict):
            return packet
        symbol = str(packet.get("symbol", "")).strip()
        identity = self._client._identity_by_symbol.get(symbol)
        if identity is None:
            return packet
        segment, security_id = identity
        try:
            ltp = float(packet.get("ltp", 0))
        except (TypeError, ValueError):
            return packet
        raw_timestamp = packet.get("exch_feed_time") or packet.get("timestamp")
        try:
            timestamp = datetime.fromtimestamp(float(raw_timestamp), tz=_IST)
            ltt = timestamp.strftime("%H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            ltt = None
        return {
            "type": "Ticker Data",
            "exchange_segment": RUNNER_SEGMENT_CODES.get(segment, -1),
            "security_id": security_id,
            "LTP": str(ltp),
            "LTT": ltt,
        }

    def subscribe_symbols(self, instruments: list[tuple[str, str]]) -> None:
        symbols = [self._client._symbol_for_identity(str(item[0]), int(item[1])) for item in instruments]
        if symbols:
            self._socket.subscribe(symbols=symbols, data_type="SymbolUpdate")

    def unsubscribe_symbols(self, instruments: list[tuple[str, str]]) -> None:
        symbols = [self._client._symbol_for_identity(str(item[0]), int(item[1])) for item in instruments]
        if symbols:
            self._socket.unsubscribe(symbols=symbols, data_type="SymbolUpdate")

    def close_connection(self) -> None:
        close = getattr(self._socket, "close_connection", None)
        if callable(close):
            close()
