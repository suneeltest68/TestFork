"""Replay the live EMA signals against historical NIFTY option premiums."""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import os
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SIGNAL_GENERATORS_DIR = _REPO_ROOT / "Signal Generators"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_SIGNAL_GENERATORS_DIR) not in sys.path:
    sys.path.insert(0, str(_SIGNAL_GENERATORS_DIR))

from ema_trend_strategy_logic import (  # noqa: E402
    EMATrendConfig,
    EMATrendPositionContext,
    EMATrendSignalEngine,
    build_ema_trend_with_indicators,
)

from Dependencies.fyers_market_data import (  # noqa: E402
    FYERS_NIFTY_SYMBOL,
    FYERS_NSE_FO_SYMBOL_MASTER_URL,
    FyersMarketDataClient,
)

_LOGGER = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")
_OPTION_RIGHTS = {"CE", "PE"}
_FIVE_SECONDS = "5S"
_HISTORY_CHUNK_DAYS = 100
_EXPIRY_LOOKAHEAD_DAYS = 45
_INDICATOR_LOOKBACK_BARS = 120
_DEFAULT_BROKERAGE_PER_TRADE = 80.0
_DEFAULT_NIFTY_LOT_SIZE = 65
_OPTION_SYMBOL_SUFFIX = re.compile(r"(?P<strike>\d+(?:\.\d+)?)(?P<right>CE|PE)$")


@dataclass(frozen=True)
class OptionContract:
    """One Fyers NIFTY option contract from the public symbol master."""

    symbol: str
    expiry: date
    strike: float
    right: str
    lot_size: int


@dataclass(frozen=True)
class SignalEvent:
    """An EMA decision and the first time it could be acted on."""

    action: str
    signal_time: datetime
    available_at: datetime
    direction: str
    spot_close: float
    reason: str


@dataclass(frozen=True)
class TradeRecord:
    """One completed option-premium trade with its source signal and fills."""

    direction: str
    symbol: str
    right: str
    strike: float
    expiry: date
    lots: int
    lot_size: int
    quantity: int
    entry_signal_time: datetime
    entry_fill_time: datetime
    entry_spot_close: float
    entry_price: float
    exit_signal_time: datetime
    exit_fill_time: datetime
    exit_price: float
    exit_reason: str
    gross_pnl: float
    brokerage_estimate: float
    net_pnl_estimate: float


def _read_history_as_ist(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert Fyers epoch timestamps to sorted, exchange-local candle rows."""
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    result = frame.copy()
    result["timestamp"] = (
        pd.to_datetime(result["timestamp"], unit="s", utc=True).dt.tz_convert(_IST).dt.tz_localize(None)
    )
    return result.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def fetch_index_1m_history(
    client: FyersMarketDataClient,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Fetch NIFTY 1-minute bars in Fyers-sized windows, including warm-up history."""
    if start_date > end_date:
        raise ValueError("Backtest start date must not be after its end date.")

    history_start = start_date - timedelta(days=21)
    chunks: list[pd.DataFrame] = []
    chunk_start = history_start
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=_HISTORY_CHUNK_DAYS - 1), end_date)
        candles = client.fetch_history(
            FYERS_NIFTY_SYMBOL,
            chunk_start,
            chunk_end,
            resolution=1,
        )
        if not candles.empty:
            chunks.append(_read_history_as_ist(candles))
        chunk_start = chunk_end + timedelta(days=1)
    if not chunks:
        raise RuntimeError(f"Fyers returned no NIFTY 1-minute candles from {history_start} to {end_date}.")
    frame = pd.concat(chunks, ignore_index=True)
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    session_times = frame["timestamp"].dt.time
    frame = frame.loc[
        (frame["timestamp"].dt.date <= end_date) & (session_times >= time(9, 15)) & (session_times < time(15, 30))
    ].reset_index(drop=True)
    return frame


def load_fyers_option_contracts() -> list[OptionContract]:
    """Read the public Fyers NSE F&O master used to choose dated NIFTY contracts."""
    response = requests.get(FYERS_NSE_FO_SYMBOL_MASTER_URL, timeout=30)
    response.raise_for_status()
    rows = csv.reader(response.content.decode("utf-8").splitlines())
    contracts: list[OptionContract] = []
    for row in rows:
        if len(row) < 17 or row[13].strip().upper() != "NIFTY":
            continue
        right = row[16].strip().upper()
        if right not in _OPTION_RIGHTS:
            continue
        try:
            expiry = datetime.fromtimestamp(float(row[8]), tz=_IST).date()
            strike = float(row[15])
            lot_size = int(float(row[3]))
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        symbol = row[9].strip()
        if symbol.startswith("NSE:") and strike > 0 and lot_size > 0:
            contracts.append(OptionContract(symbol, expiry, strike, right, lot_size))
    if not contracts:
        raise ValueError("Fyers' NSE F&O symbol master contains no valid NIFTY options.")
    return contracts


def load_fyers_expired_option_contracts(
    client: FyersMarketDataClient,
    start_date: date,
    end_date: date,
    *,
    lot_size: int = _DEFAULT_NIFTY_LOT_SIZE,
) -> list[OptionContract]:
    """Resolve historical expiries and option symbols through Fyers' expired-F&O API."""
    if lot_size <= 0:
        raise ValueError("Historical NIFTY option lot size must be positive.")
    expiry_search_end = min(
        end_date + timedelta(days=_EXPIRY_LOOKAHEAD_DAYS),
        datetime.now(_IST).date() - timedelta(days=1),
    )
    if expiry_search_end < start_date:
        return []
    expiry_dates = client.fetch_expiry_dates(
        FYERS_NIFTY_SYMBOL,
        start_date,
        expiry_search_end,
    )
    eligible_expiries = [expiry for expiry in expiry_dates if start_date <= expiry <= expiry_search_end]

    contracts: list[OptionContract] = []
    for expiry in eligible_expiries:
        symbols = client.fetch_expired_option_symbols(FYERS_NIFTY_SYMBOL, expiry)
        for symbol in symbols:
            match = _expired_option_symbol_suffix(symbol, expiry)
            if match is None:
                raise ValueError(f"Cannot parse strike and option right from Fyers contract symbol {symbol!r}.")
            contracts.append(
                OptionContract(
                    symbol=symbol,
                    expiry=expiry,
                    strike=float(match.group("strike")),
                    right=match.group("right"),
                    lot_size=lot_size,
                )
            )
    return contracts


def _expired_option_symbol_suffix(symbol: str, expiry: date) -> re.Match[str] | None:
    """Extract strike and right after stripping the expiry encoded in a Fyers symbol."""
    body = symbol.rsplit(":", 1)[-1].upper()
    if not body.startswith("NIFTY"):
        return None
    contract = body.removeprefix("NIFTY")
    month_name = expiry.strftime("%b").upper()
    expiry_codes = (
        f"{expiry:%y}{month_name}{expiry:%d}",
        f"{expiry:%y}{month_name[0]}{expiry:%d}",
        f"{expiry:%y}{month_name}",
        f"{expiry:%y}{expiry.month}{expiry:%d}",
        f"{expiry:%y}{expiry:%m}{expiry:%d}",
    )
    for expiry_code in expiry_codes:
        if contract.startswith(expiry_code):
            return _OPTION_SYMBOL_SUFFIX.fullmatch(contract[len(expiry_code) :])
    return None


def select_atm_contract(
    contracts: list[OptionContract],
    trading_date: date,
    spot_price: float,
    direction: str,
) -> OptionContract:
    """Select the second listed expiry and closest live-style ATM CE or PE."""
    right_by_direction = {"LONG": "CE", "SHORT": "PE"}
    normalized_direction = str(direction).strip().upper()
    if normalized_direction not in right_by_direction:
        raise ValueError(f"Unsupported EMA direction: {direction!r}")
    if not pd.notna(spot_price) or float(spot_price) <= 0:
        raise ValueError(f"Invalid NIFTY spot price for option selection: {spot_price!r}")

    expiries = sorted({contract.expiry for contract in contracts if contract.expiry >= trading_date})
    if len(expiries) < 2:
        raise ValueError(f"Need two Fyers NIFTY expiries on or after {trading_date}; found {len(expiries)}.")
    target_expiry = expiries[1]
    eligible = [
        contract
        for contract in contracts
        if contract.expiry == target_expiry and contract.right == right_by_direction[normalized_direction]
    ]
    if not eligible:
        raise ValueError(
            f"Fyers symbol master has no {right_by_direction[normalized_direction]} contracts for {target_expiry}."
        )
    rounded_atm = round(float(spot_price) / 50) * 50
    return min(eligible, key=lambda contract: (abs(contract.strike - rounded_atm), contract.strike))


def resample_complete_ohlc(ohlc: pd.DataFrame, timeframe_minutes: int = 5) -> pd.DataFrame:
    """Aggregate only exact, complete groups of minute candles."""
    required = {"timestamp", "open", "high", "low", "close"}
    missing = sorted(required - set(ohlc.columns))
    if missing:
        raise ValueError(f"Missing columns for EMA signal resampling: {', '.join(missing)}")
    if timeframe_minutes <= 0:
        raise ValueError("EMA timeframe must be a positive number of minutes.")

    frame = ohlc.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
    frame = frame.drop_duplicates("timestamp", keep=False).reset_index(drop=True)
    output: list[dict[str, object]] = []
    for bucket_start, group in frame.groupby(frame["timestamp"].dt.floor(f"{timeframe_minutes}min")):
        expected = pd.date_range(bucket_start, periods=timeframe_minutes, freq="min")
        if group["timestamp"].tolist() != list(expected):
            continue
        output.append(
            {
                "timestamp": bucket_start,
                "open": float(group["open"].iloc[0]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group["close"].iloc[-1]),
            }
        )
    return pd.DataFrame(output, columns=["timestamp", "open", "high", "low", "close"])


def build_signal_events(
    minute_bars: pd.DataFrame,
    *,
    start_date: date,
    end_date: date,
    config: EMATrendConfig,
    timeframe_minutes: int = 5,
    entry_start: time = time(9, 25),
    square_off: time = time(15, 15),
) -> list[SignalEvent]:
    """Run the shared EMA engine on completed 5-minute NIFTY candles."""
    five_minute = resample_complete_ohlc(minute_bars, timeframe_minutes)
    if five_minute.empty:
        raise RuntimeError("No complete 5-minute NIFTY candles were available for the backtest.")
    indicators = build_ema_trend_with_indicators(five_minute, config)
    engine = EMATrendSignalEngine(config)
    events: list[SignalEvent] = []
    position: EMATrendPositionContext | None = None
    position_date: date | None = None

    for index, candle in indicators.iterrows():
        candle_time = pd.Timestamp(candle["timestamp"]).to_pydatetime()
        trading_date = candle_time.date()
        if trading_date < start_date or trading_date > end_date:
            continue
        available_at = candle_time + timedelta(minutes=timeframe_minutes)
        if position is not None and position_date != trading_date:
            previous_day = position_date
            if previous_day is not None:
                events.append(
                    SignalEvent(
                        "EXIT",
                        datetime.combine(previous_day, square_off),
                        datetime.combine(previous_day, square_off),
                        position.direction,
                        float(candle["close"]),
                        "15:15 square-off",
                    )
                )
            position = None
            position_date = None

        if position is not None and available_at.time() >= square_off:
            events.append(
                SignalEvent(
                    "EXIT",
                    available_at,
                    available_at,
                    position.direction,
                    float(candle["close"]),
                    "15:15 square-off",
                )
            )
            position = None
            position_date = None
            continue

        prefix = indicators.iloc[max(0, index - _INDICATOR_LOOKBACK_BARS + 1) : index + 1]
        decision = engine.evaluate_candle(prefix, position=position)
        if position is not None:
            if decision.action == "EXIT":
                events.append(
                    SignalEvent(
                        "EXIT",
                        candle_time,
                        available_at,
                        position.direction,
                        float(candle["close"]),
                        decision.exit_reason,
                    )
                )
                position = None
                position_date = None
            continue

        if not entry_start <= candle_time.time() or available_at.time() >= square_off:
            continue
        if decision.action not in {"ENTER_LONG", "ENTER_SHORT"}:
            continue
        direction = "LONG" if decision.action == "ENTER_LONG" else "SHORT"
        position = EMATrendPositionContext(
            direction=direction,
            entry_underlying=float(decision.entry_underlying),
            stop_underlying=float(decision.stop_underlying),
        )
        position_date = trading_date
        events.append(
            SignalEvent(
                "ENTRY",
                candle_time,
                available_at,
                direction,
                float(decision.entry_underlying),
                "EMA entry signal",
            )
        )
    return events


def _option_candles_as_ist(frame: pd.DataFrame, trading_date: date) -> pd.DataFrame:
    """Keep regular-session option bars and expose local timestamps."""
    result = _read_history_as_ist(frame)
    if result.empty:
        return result
    times = result["timestamp"].dt.time
    result = result.loc[
        (result["timestamp"].dt.date == trading_date) & (times >= time(9, 15)) & (times < time(15, 30))
    ].reset_index(drop=True)
    return result


def _first_open_at_or_after(candles: pd.DataFrame, timestamp: datetime) -> tuple[datetime, float]:
    """Return the first available candle open after a decision becomes actionable."""
    available = candles.loc[candles["timestamp"] >= timestamp]
    if available.empty:
        raise RuntimeError(f"No option candle is available at or after {timestamp:%Y-%m-%d %H:%M:%S}.")
    row = available.iloc[0]
    return pd.Timestamp(row["timestamp"]).to_pydatetime(), float(row["open"])


def replay_option_trades(
    events: list[SignalEvent],
    *,
    contracts: list[OptionContract],
    lots: int,
    brokerage_per_trade: float,
    fetch_option_history: Callable[[str, date], pd.DataFrame],
) -> tuple[list[TradeRecord], dict[str, object] | None]:
    """Fetch an option's history only after an entry signal and pair its fills."""
    if lots <= 0:
        raise ValueError("EMA lots must be positive.")
    if brokerage_per_trade < 0:
        raise ValueError("Brokerage estimate must not be negative.")

    candle_cache: dict[tuple[str, date], pd.DataFrame] = {}
    trades: list[TradeRecord] = []
    open_trade: dict[str, object] | None = None

    for event in events:
        trading_date = event.available_at.date()
        if event.action == "ENTRY":
            if open_trade is not None:
                raise RuntimeError("Received an entry signal while an option trade is still open.")
            contract = select_atm_contract(
                contracts,
                trading_date,
                event.spot_close,
                event.direction,
            )
            key = (contract.symbol, trading_date)
            if key not in candle_cache:
                fetched = fetch_option_history(contract.symbol, trading_date)
                candle_cache[key] = _option_candles_as_ist(fetched, trading_date)
            entry_time, entry_price = _first_open_at_or_after(
                candle_cache[key],
                event.available_at,
            )
            open_trade = {
                "event": event,
                "contract": contract,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "candles": candle_cache[key],
            }
            continue

        if event.action != "EXIT":
            raise ValueError(f"Unsupported backtest event action: {event.action!r}")
        if open_trade is None:
            continue
        entry_event = open_trade["event"]
        contract = open_trade["contract"]
        entry_time = open_trade["entry_time"]
        entry_price = float(open_trade["entry_price"])
        candles = open_trade["candles"]
        exit_time, exit_price = _first_open_at_or_after(candles, event.available_at)
        quantity = contract.lot_size * lots
        gross_pnl = round((exit_price - entry_price) * quantity, 2)
        trades.append(
            TradeRecord(
                direction=entry_event.direction,
                symbol=contract.symbol,
                right=contract.right,
                strike=contract.strike,
                expiry=contract.expiry,
                lots=lots,
                lot_size=contract.lot_size,
                quantity=quantity,
                entry_signal_time=entry_event.signal_time,
                entry_fill_time=entry_time,
                entry_spot_close=entry_event.spot_close,
                entry_price=entry_price,
                exit_signal_time=event.signal_time,
                exit_fill_time=exit_time,
                exit_price=exit_price,
                exit_reason=event.reason,
                gross_pnl=gross_pnl,
                brokerage_estimate=brokerage_per_trade,
                net_pnl_estimate=round(gross_pnl - brokerage_per_trade, 2),
            )
        )
        open_trade = None

    open_summary = None
    if open_trade is not None:
        event = open_trade["event"]
        contract = open_trade["contract"]
        open_summary = {
            "direction": event.direction,
            "symbol": contract.symbol,
            "entry_time": open_trade["entry_time"].isoformat(),
            "entry_price": open_trade["entry_price"],
            "entry_signal_time": event.signal_time.isoformat(),
            "status": "OPEN - no EMA exit or square-off signal in supplied data",
        }
    return trades, open_summary


def _environment_int(name: str, default: int) -> int:
    """Read a whole-number setting, failing clearly on malformed values."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number.") from exc
    if not math.isfinite(value) or not value.is_integer():
        raise ValueError(f"{name} must be a whole number.")
    return int(value)


def _environment_float(name: str, default: float) -> float:
    """Read a numeric setting, failing clearly on malformed values."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite.")
    return value


def _scaled_ema_lots_from_environment() -> int:
    """Match the live EMA lot setting and its bounded size multiplier."""
    try:
        configured_multiplier = float(os.getenv("EMA_SIZE_MULTIPLIER", "1"))
    except (TypeError, ValueError):
        multiplier = 1
    else:
        multiplier = (
            int(configured_multiplier)
            if math.isfinite(configured_multiplier)
            and configured_multiplier.is_integer()
            and 1 <= configured_multiplier <= 25
            else 1
        )
    return _environment_int("EMA_LOTS", 1) * multiplier


def _ema_config_from_environment() -> EMATrendConfig:
    """Build the same tunable EMA configuration the live worker reads."""
    return EMATrendConfig(
        ema_fast_period=_environment_int("EMA_TREND_FAST_PERIOD", 4),
        ema_mid_period=_environment_int("EMA_TREND_MID_PERIOD", 11),
        ema_slow_period=_environment_int("EMA_TREND_SLOW_PERIOD", 18),
        atr_period=_environment_int("EMA_TREND_ATR_PERIOD", 14),
        adx_period=_environment_int("EMA_TREND_ADX_PERIOD", 14),
        slope_lookback=_environment_int("EMA_TREND_SLOPE_LOOKBACK", 3),
        adx_threshold=_environment_float("EMA_TREND_ADX_THRESHOLD", 20.0),
        distance_atr_multiplier=_environment_float("EMA_TREND_DISTANCE_ATR_MULT", 0.5),
        ema11_slope_atr_multiplier=_environment_float("EMA_TREND_EMA11_SLOPE_ATR_MULT", 0.3),
        ema18_slope_atr_multiplier=_environment_float("EMA_TREND_EMA18_SLOPE_ATR_MULT", 0.2),
    )


def _write_outputs(
    trades: list[TradeRecord],
    open_trade: dict[str, object] | None,
    *,
    output_dir: Path,
    start_date: date,
    end_date: date,
    lots: int,
    timeframe_minutes: int,
    brokerage_per_trade: float,
    option_resolution: str = _FIVE_SECONDS,
    expiry_source: str = "current Fyers symbol master",
) -> tuple[Path, Path]:
    """Write the trade ledger, a readable HTML report, and a concise run summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{start_date.isoformat()}_to_{end_date.isoformat()}"
    trades_path = output_dir / f"ema_options_{suffix}_trades.csv"
    summary_path = output_dir / f"ema_options_{suffix}_summary.json"
    report_path = output_dir / f"ema_options_{suffix}_report.html"
    resolution_label = "5-second" if option_resolution == _FIVE_SECONDS else f"{option_resolution}-minute"
    trade_rows: list[dict[str, object]] = []
    cumulative_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0
    for trade in trades:
        cumulative_pnl = round(cumulative_pnl + trade.net_pnl_estimate, 2)
        peak_pnl = max(peak_pnl, cumulative_pnl)
        drawdown = round(cumulative_pnl - peak_pnl, 2)
        max_drawdown = min(max_drawdown, drawdown)
        trade_rows.append({
            key: value.isoformat() if isinstance(value, (datetime, date)) else value
            for key, value in asdict(trade).items()
        } | {
            "cumulative_pnl": cumulative_pnl,
            "drawdown_from_peak": drawdown,
            "max_drawdown_to_date": round(abs(max_drawdown), 2),
        })
    trade_columns = [
        *TradeRecord.__dataclass_fields__,
        "cumulative_pnl",
        "drawdown_from_peak",
        "max_drawdown_to_date",
    ]
    pd.DataFrame(trade_rows, columns=trade_columns).to_csv(trades_path, index=False)
    _write_html_report(
        report_path,
        trades=trades,
        trade_rows=trade_rows,
        start_date=start_date,
        end_date=end_date,
        max_drawdown=abs(max_drawdown),
    )
    summary = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "strategy": "Live EMA signal engine on complete NIFTY 1-minute-to-5-minute bars",
        "option_resolution": option_resolution,
        "option_fill_rule": (f"first Fyers {resolution_label} option candle open at/after completed signal bar"),
        "expiry_rule": "second Fyers-listed NIFTY expiry on or after each entry date",
        "expiry_source": expiry_source,
        "direction_mapping": "LONG buys CE; SHORT buys PE",
        "timeframe_minutes": timeframe_minutes,
        "lots": lots,
        "lot_size": trades[0].lot_size if trades else _DEFAULT_NIFTY_LOT_SIZE,
        "closed_trades": len(trades),
        "gross_pnl": round(sum(trade.gross_pnl for trade in trades), 2),
        "estimated_brokerage": round(sum(trade.brokerage_estimate for trade in trades), 2),
        "net_pnl_estimate": round(sum(trade.net_pnl_estimate for trade in trades), 2),
        "max_drawdown": round(abs(max_drawdown), 2),
        "cost_note": (
            f"Uses the configured estimate of Rs {brokerage_per_trade:.2f} per closed trade; "
            "this is not a broker-specific options tax and fee calculation."
        ),
        "lot_size_note": (
            "The expired-contract API returns symbols but not historical lot sizes; "
            f"this replay assumes {_DEFAULT_NIFTY_LOT_SIZE} units per NIFTY lot."
        ),
        "risk_note": (
            "EMA signals and 15:15 square-off are replayed. The live worker's asynchronous "
            "rupee max-loss monitor and broker execution slippage are not simulated."
        ),
        "open_trade": open_trade,
        "trades_file": str(trades_path),
        "report_file": str(report_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return trades_path, summary_path


def _write_html_report(
    report_path: Path,
    *,
    trades: list[TradeRecord],
    trade_rows: list[dict[str, object]],
    start_date: date,
    end_date: date,
    max_drawdown: float,
) -> None:
    """Render a standalone, browser-friendly report for the completed trades."""
    total_net_pnl = round(sum(trade.net_pnl_estimate for trade in trades), 2)

    def money(value: float) -> str:
        return f"-₹{abs(value):,.2f}" if value < 0 else f"₹{value:,.2f}"

    body_rows: list[str] = []
    for trade, row in zip(trades, trade_rows):
        pnl_class = "positive" if trade.net_pnl_estimate >= 0 else "negative"
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(trade.entry_fill_time.strftime('%Y-%m-%d %H:%M:%S'))}</td>"
            f"<td>{html.escape(trade.exit_fill_time.strftime('%Y-%m-%d %H:%M:%S'))}</td>"
            f"<td class=\"option\">{html.escape(trade.symbol)}</td>"
            f"<td>{html.escape(trade.expiry.isoformat())}</td>"
            f"<td class=\"number {pnl_class}\">{money(trade.net_pnl_estimate)}</td>"
            f"<td class=\"number\">{money(float(row['cumulative_pnl']))}</td>"
            f"<td class=\"number negative\">{money(float(row['drawdown_from_peak']))}</td>"
            f"<td class=\"number\">{money(float(row['max_drawdown_to_date']))}</td>"
            "</tr>"
        )
    if not body_rows:
        body_rows.append('<tr><td class="empty" colspan="8">No closed trades in this date range.</td></tr>')

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EMA Options Backtest Report</title>
  <style>
    :root {{ color-scheme: light; --ink: #172033; --muted: #697586; --line: #e6eaf0; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 36px 20px; background: #f3f6fb; color: var(--ink);
      font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
    main {{ max-width: 1260px; margin: 0 auto; }}
    h1 {{ margin: 0; font-size: clamp(25px, 4vw, 36px); letter-spacing: -0.04em; }}
    .subtitle {{ margin: 7px 0 26px; color: var(--muted); }}
    .cards {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-bottom: 22px; }}
    .card {{ padding: 19px 21px; border: 1px solid var(--line); border-radius: 14px; background: white;
      box-shadow: 0 5px 18px #1720330a; }}
    .label {{ color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }}
    .value {{ margin-top: 5px; font-size: clamp(21px, 3vw, 29px); font-weight: 750; letter-spacing: -.03em; }}
    .positive {{ color: #087443; }} .negative {{ color: #c03535; }}
    .panel {{ overflow: hidden; border: 1px solid var(--line); border-radius: 14px; background: white;
      box-shadow: 0 5px 18px #1720330a; }}
    .panel h2 {{ margin: 0; padding: 18px 20px; font-size: 16px; }}
    .scroll {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; white-space: nowrap; }}
    th, td {{ padding: 13px 16px; border-top: 1px solid var(--line); text-align: left; }}
    th {{ background: #f8fafc; color: #526174; font-size: 11px; letter-spacing: .05em; text-transform: uppercase; }}
    td {{ font-variant-numeric: tabular-nums; }}
    .number {{ text-align: right; }} .option {{ font-weight: 650; }}
    .empty {{ padding: 38px 16px; color: var(--muted); text-align: center; }}
    .note {{ margin: 14px 3px 0; color: var(--muted); font-size: 12px; }}
    @media (max-width: 650px) {{ body {{ padding: 24px 12px; }} .cards {{ grid-template-columns: 1fr; gap: 9px; }} }}
  </style>
</head>
<body>
  <main>
    <h1>EMA Options Backtest</h1>
    <p class="subtitle">{start_date.isoformat()} to {end_date.isoformat()} · {len(trades)} closed trades · net P&amp;L after estimated brokerage</p>
    <section class="cards" aria-label="Backtest summary">
      <div class="card"><div class="label">Net P&amp;L</div><div class="value {'positive' if total_net_pnl >= 0 else 'negative'}">{money(total_net_pnl)}</div></div>
      <div class="card"><div class="label">Max drawdown</div><div class="value negative">{money(-max_drawdown)}</div></div>
      <div class="card"><div class="label">Closed trades</div><div class="value">{len(trades)}</div></div>
    </section>
    <section class="panel">
      <h2>Trade-by-trade performance</h2>
      <div class="scroll">
        <table>
          <thead><tr><th>Entry time</th><th>Exit time</th><th>Option used</th><th>Expiry date</th>
            <th class="number">Net P&amp;L</th><th class="number">Cumulative P&amp;L</th>
            <th class="number">Drawdown from peak</th><th class="number">Max DD so far</th></tr></thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
      </div>
    </section>
    <p class="note">Drawdown is measured on cumulative net P&amp;L after the estimated per-trade brokerage.
      Max drawdown is the largest peak-to-trough decline in this trade sequence. This report does not
      model broker execution slippage or the live worker's asynchronous max-loss monitor.</p>
  </main>
</body>
</html>
"""
    report_path.write_text(document, encoding="utf-8")


def run_backtest(
    start_date: date,
    end_date: date,
    *,
    output_dir: Path,
    lots: int | None = None,
    brokerage_per_trade: float | None = None,
) -> tuple[Path, Path]:
    """Fetch Fyers candles, replay EMA signals, and persist the option trades."""
    load_dotenv(_REPO_ROOT / "Dependencies" / ".env", override=False)
    client_id = os.getenv("FYERS_CLIENT_ID", "").strip()
    access_token = os.getenv("FYERS_ACCESS_TOKEN", "").strip()
    if not client_id or not access_token:
        raise ValueError("Set FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN in Dependencies/.env before running.")
    timeframe_minutes = _environment_int("EMA_DERIVED_TIMEFRAME_MINUTES", 5)
    if timeframe_minutes != 5:
        raise ValueError(
            "This options backtest currently requires EMA_DERIVED_TIMEFRAME_MINUTES=5 "
            "to match the requested live signal timeframe."
        )
    configured_lots = _scaled_ema_lots_from_environment()
    configured_brokerage = _environment_float(
        "BROKERAGE",
        _environment_float("BROGERAGE", _DEFAULT_BROKERAGE_PER_TRADE),
    )
    lots = configured_lots if lots is None else lots
    brokerage_per_trade = configured_brokerage if brokerage_per_trade is None else brokerage_per_trade

    client = FyersMarketDataClient(
        client_id,
        access_token,
        str(_REPO_ROOT / "Dependencies" / "all_instrument *.csv"),
        _REPO_ROOT / "Dependencies" / "fyers_nse_fo.csv",
        load_symbol_mappings=False,
    )
    client.validate_session()
    minute_bars = fetch_index_1m_history(client, start_date, end_date)
    events = build_signal_events(
        minute_bars,
        start_date=start_date,
        end_date=end_date,
        config=_ema_config_from_environment(),
        timeframe_minutes=timeframe_minutes,
        entry_start=time(
            _environment_int("EMA_TRADING_START_HOUR", 9),
            _environment_int("EMA_TRADING_START_MINUTE", 25),
        ),
        square_off=time(
            _environment_int("EMA_SQUARE_OFF_HOUR", 15),
            _environment_int("EMA_SQUARE_OFF_MINUTE", 15),
        ),
    )
    option_resolution: int | str = 1 if start_date < datetime.now(_IST).date() - timedelta(days=30) else _FIVE_SECONDS
    historical_contracts = load_fyers_expired_option_contracts(
        client,
        start_date,
        end_date,
    )
    current_contracts = load_fyers_option_contracts()
    contracts_by_symbol = {contract.symbol: contract for contract in historical_contracts}
    contracts_by_symbol.update({contract.symbol: contract for contract in current_contracts})
    contracts = list(contracts_by_symbol.values())
    if not contracts:
        raise ValueError("Fyers returned no historical or currently listed NIFTY option contracts.")
    expiry_source = "Fyers expired F&O APIs plus current NSE F&O symbol master"
    expired_expiries = {contract.expiry for contract in historical_contracts}

    def fetch_option_history(symbol: str, trading_date: date) -> pd.DataFrame:
        contract = contracts_by_symbol.get(symbol)
        if contract is None:
            raise RuntimeError(f"Selected option contract {symbol} is missing from the contract map.")
        use_expired_data = contract.expiry < datetime.now(_IST).date()
        _LOGGER.info(
            "Fetching %s option history after EMA entry signal: %s %s",
            option_resolution,
            symbol,
            trading_date,
        )
        if use_expired_data and contract.expiry not in expired_expiries:
            raise RuntimeError(
                f"Fyers expired-contract API did not return symbols for selected expiry {contract.expiry}."
            )
        fetch_history = client.fetch_expired_option_history if use_expired_data else client.fetch_history
        return fetch_history(symbol, trading_date, trading_date, resolution=option_resolution)

    trades, open_trade = replay_option_trades(
        events,
        contracts=contracts,
        lots=lots,
        brokerage_per_trade=brokerage_per_trade,
        fetch_option_history=fetch_option_history,
    )
    return _write_outputs(
        trades,
        open_trade,
        output_dir=output_dir,
        start_date=start_date,
        end_date=end_date,
        lots=lots,
        timeframe_minutes=timeframe_minutes,
        brokerage_per_trade=brokerage_per_trade,
        option_resolution=str(option_resolution),
        expiry_source=expiry_source,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for running an options-premium EMA backtest."""
    parser = argparse.ArgumentParser(
        description=("Replay live EMA signals on NIFTY 5-minute candles and price option trades from Fyers history.")
    )
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--lots", type=int, help="Override EMA_LOTS from Dependencies/.env.")
    parser.add_argument(
        "--brokerage-per-trade",
        type=float,
        help="Override BROKERAGE/BROGERAGE; defaults to Rs 80 per completed trade.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_REPO_ROOT / "Backtest Outputs",
        help="Where to write the trade CSV and summary JSON.",
    )
    args = parser.parse_args(argv)
    end_date = args.end_date or args.start_date
    if args.start_date > end_date:
        parser.error("--start-date must not be after --end-date.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    trades_path, summary_path = run_backtest(
        args.start_date,
        end_date,
        output_dir=args.output_dir,
        lots=args.lots,
        brokerage_per_trade=args.brokerage_per_trade,
    )
    print(f"EMA options backtest trades: {trades_path}")
    print(f"EMA options backtest summary: {summary_path}")
    report_path = summary_path.with_name(summary_path.name.replace("_summary.json", "_report.html"))
    print(f"EMA options backtest report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
