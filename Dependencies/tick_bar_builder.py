"""Pure tick-to-bar helpers for normalized WebSocket market-data packets.

The multithreaded runner can source its market data from a Fyers WebSocket feed
instead of REST polling (``MARKET_DATA_SOURCE=WEBSOCKET``). The Fyers adapter
translates provider packets into the runner's established packet shape, then a
"pump" thread and supervisor thread build the same 1-minute OHLC frames and LTP
updates as the REST producer. Everything in this module is deliberately free of
broker SDKs and network or store dependencies so it can be unit-tested offline:

* :func:`parse_marketfeed_packet` — turn one raw ``get_data()`` payload into a
  validated :class:`TickEvent` (or ``None`` for the many non-price payloads).
* :func:`packet_confirms_subscription` — recognise price and legacy
  ``Previous Close`` packets that identify a subscribed instrument.
* :func:`resolve_tick_minute` — decide which 1-minute session bucket a tick
  belongs to, rejecting the stale snapshot ticks and after-hours index
  recomputations observed on the real feed (probe run 2026-07-21).
* :class:`TickBarAggregator` — lock-guarded minute→OHLC accumulator shared by
  the pump (writer) and supervisor (reader) threads.
* :func:`merge_official_and_tick_frames` — combine REST "official" candles
  (warmup history + per-minute true-ups) with locally built tick bars; the
  official candle always wins for a minute both sides know about.
* :func:`divergence_stats` — quantify tick-vs-official candle disagreement so
  every true-up can log how faithful the local bars were.

The packet names are the runner's existing compatibility contract
(``'Ticker Data'``, ``'Quote Data'``, ``'Full Data'``, ``'Previous Close'``).
Prices are strings and ``LTT`` is an IST ``HH:MM:SS`` value; the Fyers adapter
normalizes its provider-specific fields to this shape before parsing.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time

import pandas as pd

# Canonical frame columns published to SharedMarketDataStore.  The runner's
# central frames intentionally carry no volume column (the NIFTY index reports
# none), so tick bars do not either.
OHLC_COLUMNS = ["timestamp", "open", "high", "low", "close"]

# Compatibility exchange-segment codes <-> runner segment strings.
SEGMENT_CODE_TO_NAME = {0: "IDX_I", 2: "NSE_FNO", 8: "BSE_FNO"}
SEGMENT_NAME_TO_FEED_CODE = {name: code for code, name in SEGMENT_CODE_TO_NAME.items()}

# NSE cash session bounds.  Bars may only exist for minutes in
# [09:15, 15:30); the last valid 1-minute bucket of a day is 15:29.
SESSION_FIRST_MINUTE = dt_time(9, 15)
SESSION_END = dt_time(15, 30)

# Packet ``type`` strings that carry a tradeable price (LTP)...
_PRICE_PACKET_TYPES = frozenset({"Ticker Data", "Quote Data", "Full Data"})
# ...and the wider set that identifies a subscribed instrument. `Previous
# Close` remains accepted for compatibility with the earlier provider adapter.
_CONFIRMING_PACKET_TYPES = _PRICE_PACKET_TYPES | {"Previous Close"}


@dataclass(frozen=True)
class TickEvent:
    """One validated price tick decoded from a marketfeed packet."""

    segment: str
    """Runner-style exchange segment, e.g. ``"IDX_I"`` or ``"NSE_FNO"``."""

    security_id: int
    """Runner contract id of the instrument that ticked."""

    ltp: float
    """Last traded price, already validated finite and positive."""

    ltt_raw: str | None
    """Raw ``HH:MM:SS`` last-trade-time string, or ``None`` if absent."""

    received_at: datetime
    """Naive-IST wall clock at the moment the packet was received."""


@dataclass(frozen=True)
class TrueUpDivergence:
    """How far the locally built tick bars strayed from official candles."""

    overlapping: int
    """Number of completed minutes both frames had a bar for."""

    mismatched: int
    """How many of those minutes disagreed on any OHLC field."""

    max_abs_delta: float
    """Largest absolute OHLC difference seen across the overlap."""


def _decode_instrument(packet: dict) -> tuple[str, int] | None:
    """Return ``(segment_name, security_id)`` from a packet, or ``None``."""

    raw_segment = packet.get("exchange_segment")
    segment = SEGMENT_CODE_TO_NAME.get(raw_segment) if isinstance(raw_segment, int) else None
    if segment is None:
        return None
    try:
        security_id = int(packet.get("security_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return segment, security_id


def parse_marketfeed_packet(packet: object, now_ist: datetime) -> TickEvent | None:
    """Decode one normalized market-feed packet into a tick.

    Returns ``None`` for everything that is not a well-formed price packet:
    ``None`` payloads, status strings ("Markets Open"), 'Previous Close' /
    'Market Depth' / 'OI Data' packets, unknown exchange segments, and prices
    that are missing, non-numeric, non-finite, or not strictly positive. The
    Fyers adapter translates provider messages into this runner packet shape.
    """

    if not isinstance(packet, dict):
        return None
    if packet.get("type") not in _PRICE_PACKET_TYPES:
        return None
    instrument = _decode_instrument(packet)
    if instrument is None:
        return None
    try:
        ltp = float(packet.get("LTP"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ltp) or ltp <= 0.0:
        return None
    ltt = packet.get("LTT")
    return TickEvent(
        segment=instrument[0],
        security_id=instrument[1],
        ltp=ltp,
        ltt_raw=ltt if isinstance(ltt, str) else None,
        received_at=now_ist,
    )


def packet_confirms_subscription(packet: object) -> tuple[str, int] | None:
    """Identify packets that indicate an instrument subscription is active.

    Any price packet counts, and legacy ``Previous Close`` snapshots remain
    accepted. Returns the ``(segment_name, security_id)`` key the runner uses,
    or ``None`` when the payload identifies no subscribed instrument.
    """

    if not isinstance(packet, dict):
        return None
    if packet.get("type") not in _CONFIRMING_PACKET_TYPES:
        return None
    return _decode_instrument(packet)


def resolve_tick_minute(
    ltt_raw: object,
    now_ist: datetime,
    tolerance_seconds: float = 90.0,
) -> pd.Timestamp | None:
    """Map a tick's ``LTT`` to the 1-minute session bucket it belongs to.

    The normalized ``LTT`` is an IST ``HH:MM:SS`` time-of-day string. A tick is
    only allowed to build bars when its LTT is BOTH:

    * within ``tolerance_seconds`` of the local IST clock — this rejects stale
      snapshots carrying an earlier session's timestamp, and
    * inside the NSE session window [09:15, 15:30) — this rejects after-hours
      index recomputations (observed stamping e.g. 19:26:03).

    Rejected ticks return ``None``; callers still use their LTP for the LTP
    cache, they just never become candles.
    """

    try:
        tick_time = datetime.strptime(str(ltt_raw), "%H:%M:%S").time()
    except ValueError:
        return None
    candidate = datetime.combine(now_ist.date(), tick_time)
    if abs((candidate - now_ist).total_seconds()) > tolerance_seconds:
        return None
    if tick_time < SESSION_FIRST_MINUTE or tick_time >= SESSION_END:
        return None
    return pd.Timestamp(candidate).floor("min")


class TickBarAggregator:
    """Lock-guarded accumulator of 1-minute OHLC bars built from ticks.

    The websocket pump thread calls :meth:`add_tick`; the supervisor thread
    reads :meth:`tick_bars_frame` / :meth:`signature` and prunes.  A single
    internal lock keeps every mutation and snapshot atomic; the aggregator is
    bounded so a long-running session cannot grow without limit.
    """

    def __init__(self, max_minutes: int = 3 * 375) -> None:
        # 375 one-minute buckets per NSE session; keep roughly three days so
        # gaps in the official REST history can still be tick-filled.
        self._lock = threading.Lock()
        self._bars: dict[pd.Timestamp, list[float]] = {}
        self._version = 0
        self._max_minutes = max_minutes

    def add_tick(self, minute_ts: pd.Timestamp, ltp: float) -> bool:
        """Fold one tick into its minute bar.

        Returns ``True`` exactly when this tick STARTED a new newest bar (a
        minute rollover) — the supervisor uses that to publish immediately.
        Late ticks that update or gap-fill an older minute return ``False``.
        """

        with self._lock:
            self._version += 1
            bar = self._bars.get(minute_ts)
            if bar is not None:
                bar[1] = max(bar[1], ltp)
                bar[2] = min(bar[2], ltp)
                bar[3] = ltp
                return False
            newest = max(self._bars) if self._bars else None
            self._bars[minute_ts] = [ltp, ltp, ltp, ltp]
            if len(self._bars) > self._max_minutes:
                del self._bars[min(self._bars)]
            return newest is None or minute_ts > newest

    def tick_bars_frame(self) -> pd.DataFrame:
        """Snapshot all bars as a sorted canonical OHLC frame."""

        with self._lock:
            items = sorted(self._bars.items())
        if not items:
            return pd.DataFrame(columns=OHLC_COLUMNS)
        return pd.DataFrame(
            {
                "timestamp": [ts for ts, _ in items],
                "open": [bar[0] for _, bar in items],
                "high": [bar[1] for _, bar in items],
                "low": [bar[2] for _, bar in items],
                "close": [bar[3] for _, bar in items],
            }
        )

    def signature(self) -> int:
        """Cheap change marker: bumps on every mutation, stable when idle."""

        with self._lock:
            return self._version

    def prune_older_than(self, cutoff_ts: pd.Timestamp) -> None:
        """Drop bars strictly older than ``cutoff_ts``."""

        with self._lock:
            keep = {ts: bar for ts, bar in self._bars.items() if ts >= cutoff_ts}
            if len(keep) != len(self._bars):
                self._bars = keep
                self._version += 1


def merge_official_and_tick_frames(
    official: pd.DataFrame, tick_bars: pd.DataFrame
) -> pd.DataFrame:
    """Combine official REST candles with locally built tick bars.

    The official frame (warmup history plus per-minute true-ups) wins whenever
    both sides have a bar for the same minute; tick bars fill every minute the
    official history does not cover yet — most importantly the currently
    forming minute, and any holes.  Inputs are never mutated; extra columns
    (e.g. a REST volume column) are dropped so the published frame always has
    the canonical OHLC shape.
    """

    pieces = [
        frame.loc[:, OHLC_COLUMNS]
        for frame in (official, tick_bars)
        if frame is not None and not frame.empty
    ]
    if not pieces:
        return pd.DataFrame(columns=OHLC_COLUMNS)
    merged = pd.concat(pieces, ignore_index=True)
    merged = merged.drop_duplicates(subset="timestamp", keep="first")
    return merged.sort_values("timestamp", kind="stable").reset_index(drop=True)


def divergence_stats(
    official: pd.DataFrame,
    tick_bars: pd.DataFrame,
    forming_minute: pd.Timestamp | None = None,
) -> TrueUpDivergence:
    """Measure how much the tick bars disagreed with official candles.

    Compares OHLC on the minutes both frames cover, excluding the (still
    changing) forming minute when given.  Logged on every true-up so the
    operator can quantify tick-bar fidelity from the paper sessions before
    trusting the websocket path with live entries.
    """

    if official.empty or tick_bars.empty:
        return TrueUpDivergence(0, 0, 0.0)
    official_by_minute = official.set_index("timestamp")
    tick_by_minute = tick_bars.set_index("timestamp")
    common = official_by_minute.index.intersection(tick_by_minute.index)
    if forming_minute is not None:
        common = common.difference([forming_minute])
    if len(common) == 0:
        return TrueUpDivergence(0, 0, 0.0)
    price_columns = OHLC_COLUMNS[1:]
    deltas = (
        official_by_minute.loc[common, price_columns]
        - tick_by_minute.loc[common, price_columns]
    ).abs()
    worst_per_minute = deltas.max(axis=1)
    return TrueUpDivergence(
        overlapping=len(common),
        mismatched=int((worst_per_minute > 1e-9).sum()),
        max_abs_delta=float(worst_per_minute.max()),
    )
