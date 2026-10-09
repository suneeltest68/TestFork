"""Regression tests for the Fyers-backed resumable index-history fetcher."""

from __future__ import annotations

import importlib.util
import os
import subprocess  # nosec B404 - launching the wrapper as a script IS what this test asserts
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

# Tests/Data Extractors/<this file> -> the repository root is two levels up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = _REPO_ROOT / "Data Extractors" / "index_1m_5y_data_fetch_fyers_common.py"
spec = importlib.util.spec_from_file_location("index_1m_5y_data_fetch_fyers_common", MODULE_PATH)
fetcher = importlib.util.module_from_spec(spec)
sys.modules["index_1m_5y_data_fetch_fyers_common"] = fetcher
spec.loader.exec_module(fetcher)


def _args():
    # Built from a dict (not keyword args) so Bandit's B106 doesn't read the
    # dummy `access_token` literal as a hardcoded password.
    fields = {
        "client_id": "CLIENT123", "access_token": "dummy-token",
        "exchange_segment": "IDX_I", "security_id": 13, "instrument_type": "INDEX",
        "interval": 1, "chunk_days": 5, "sleep_seconds": 0,
    }
    return SimpleNamespace(**fields)


def test_fetch_builds_fyers_client_without_loading_option_masters():
    defaults = SimpleNamespace(display_name="NIFTY")
    fake_client = object()
    with (
        patch.object(fetcher, "FyersMarketDataClient", return_value=fake_client) as client_ctor,
        patch.object(fetcher, "resolve_date_range", return_value=(date(2026, 1, 1), date(2026, 1, 1))),
        patch.object(fetcher, "fetch_chunk", return_value=pd.DataFrame()) as chunk,
    ):
        result = fetcher.fetch_1m_history(_args(), defaults)

    client_ctor.assert_called_once()
    assert client_ctor.call_args.args[:2] == ("CLIENT123", "dummy-token")
    assert client_ctor.call_args.kwargs["load_symbol_mappings"] is False
    chunk.assert_called_once()
    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_access_token_is_environment_only_never_a_cli_flag(monkeypatch):
    """MAT-108: a secret typed on the command line lands in shell history."""

    defaults = fetcher.IndexFetchDefaults(
        display_name="NIFTY",
        security_id="13",
        default_output="out.csv",
    )
    # Hermetic: the engine now load_dotenv()s Dependencies/.env at import, so
    # every key this assertion depends on is pinned here. Leaving one unset
    # would let a real token decide the result -- and print it on failure.
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", "env-only-token")
    monkeypatch.setattr(sys, "argv", ["fetcher"])

    args = fetcher.parse_args(defaults)
    assert args.access_token == "env-only-token"

    # The old --access-token flag must be gone: argparse rejects it (exit 2).
    monkeypatch.setattr(sys, "argv", ["fetcher", "--access-token", "cli-token"])
    with pytest.raises(SystemExit):
        fetcher.parse_args(defaults)


def _payload(timestamps, *, close=None):
    count = len(timestamps)
    return {
        "timestamp": timestamps,
        "open": [100.0] * count,
        "high": [102.0] * count,
        "low": [99.0] * count,
        "close": close or [101.0] * count,
        "volume": [1000.0] * count,
    }


def test_normalize_rejects_mixed_epoch_units_and_non_finite_candles():
    second = int(datetime(2026, 1, 1, 9, 15).timestamp())
    with pytest.raises(fetcher.MarketDataValidationError):
        fetcher.normalize_response_data(_payload([second, (second + 60) * 1000]))

    with pytest.raises(fetcher.MarketDataValidationError):
        fetcher.normalize_response_data(
            _payload([second, second + 60], close=[101.0, float("inf")])
        )


def test_fetch_chunk_rejects_timestamp_outside_requested_window():
    # A WEEKDAY outside the window: the session clip drops weekend rows before
    # the window check ever runs, so a Saturday would prove nothing here.
    outside = int(datetime(2026, 1, 5, 9, 15).timestamp())
    client = SimpleNamespace(
        fetch_index_history=lambda *_args, **_kwargs: pd.DataFrame(_payload([outside]))
    )
    with pytest.raises(fetcher.MarketDataValidationError, match="outside requested chunk"):
        fetcher.fetch_chunk(
            client=client,
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            interval=1,
            chunk_start=date(2026, 1, 1),
            chunk_end=date(2026, 1, 2),
        )


def test_normalizer_accepts_fyers_history_dataframe_output():
    payload = _session_payload(["2026-09-15 09:15:00", "2026-09-15 09:16:00"])

    frame = fetcher.normalize_response_data(pd.DataFrame(payload), instrument_type="INDEX")

    assert [str(value) for value in frame["timestamp"]] == [
        "2026-09-15 09:15:00",
        "2026-09-15 09:16:00",
    ]


def test_normalizer_sorts_reverse_chronological_fyers_history():
    payload = _session_payload(["2026-09-15 09:16:00", "2026-09-15 09:15:00"])

    frame = fetcher.normalize_response_data(pd.DataFrame(payload), instrument_type="INDEX")

    assert [str(value) for value in frame["timestamp"]] == [
        "2026-09-15 09:15:00",
        "2026-09-15 09:16:00",
    ]
    assert frame["timestamp"].is_monotonic_increasing


def test_atomic_csv_replace_preserves_existing_file_on_write_failure(tmp_path):
    target = tmp_path / "history.csv"
    target.write_text("old-safe-data\n", encoding="utf-8")
    frame = pd.DataFrame({"timestamp": ["2026-01-01"], "close": [100.0]})

    with (
        patch.object(pd.DataFrame, "to_csv", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        fetcher.atomic_write_csv(frame, target)

    assert target.read_text(encoding="utf-8") == "old-safe-data\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_csv_replace_commits_complete_file(tmp_path):
    target = tmp_path / "history.csv"
    target.write_text("old-safe-data\n", encoding="utf-8")

    fetcher.atomic_write_csv(pd.DataFrame({"close": [101.0]}), target)

    assert "101.0" in target.read_text(encoding="utf-8")


def test_wrapper_runs_as_a_script_the_way_algo_py_launches_it():
    """`algo.py fetch-data` runs the wrapper as a script, not as an import.

    Python then puts the SCRIPT's folder on sys.path -- not the repository root,
    and not the cwd algo.py sets -- so the engine's `from Dependencies....`
    import used to die with ModuleNotFoundError before argparse ever ran. This
    test reproduces that exact launch and asserts the module imports.

    `--help` exits 0 without credentials or network, which is enough: the old
    failure happened at import time, well before argument parsing.
    """
    wrapper = _REPO_ROOT / "Data Extractors" / "nifty_1m_5y_data_fetch_fyers.py"

    # Strip PYTHONPATH so a developer's own path setup cannot mask the bug, and
    # clear the credential keys so nothing here depends on a populated .env.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.pop("FYERS_CLIENT_ID", None)
    env.pop("FYERS_ACCESS_TOKEN", None)

    # nosec B603 - argv is [sys.executable, <a path built from __file__>, "--help"];
    # nothing here comes from user input, and no shell is involved.
    completed = subprocess.run(  # nosec B603
        [sys.executable, str(wrapper), "--help"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert "ModuleNotFoundError" not in completed.stderr, completed.stderr
    assert completed.returncode == 0, completed.stderr
    assert "--lookback" in completed.stdout


def test_fyers_access_token_is_read_from_environment_only(monkeypatch):
    defaults = fetcher.IndexFetchDefaults(
        display_name="NIFTY", security_id="13", default_output="out.csv"
    )
    monkeypatch.setattr(sys, "argv", ["prog"])
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", "fyers-token")
    assert fetcher.parse_args(defaults).access_token == "fyers-token"
    monkeypatch.delenv("FYERS_ACCESS_TOKEN")
    assert fetcher.parse_args(defaults).access_token == ""


def _session_payload(times, closes=None):
    """An OHLC payload with Unix-second timestamps for IST wall-clock bars."""

    stamps = [pd.Timestamp(value) for value in times]
    epoch = [
        int((stamp.tz_localize("Asia/Kolkata")).timestamp()) for stamp in stamps
    ]
    closes = closes or [100.0 + index for index in range(len(stamps))]
    return {
        "timestamp": epoch,
        "open": closes,
        "high": [value + 1.0 for value in closes],
        "low": [value - 1.0 for value in closes],
        "close": closes,
        "volume": [0.0] * len(closes),
    }


def test_bars_outside_the_session_are_dropped_before_validation():
    """Historical windows may include rows outside the market session.

    These rows are minute-ALIGNED, so the validator accepts them. That makes
    them more dangerous than malformed rows, not less: left in, they drag a
    day's high, low and close with them.
    """

    frame = fetcher.normalize_response_data(
        _session_payload([
            "2026-09-15 09:00:00",   # pre-open
            "2026-09-15 09:15:00",
            "2026-09-15 15:29:00",
            "2026-09-15 16:45:00",   # after the close
            "2026-09-15 17:59:00",   # the far end of the old-era junk
        ])
    )

    kept = [str(value) for value in frame["timestamp"]]
    assert kept == ["2026-09-15 09:15:00", "2026-09-15 15:29:00"]


def test_the_synthetic_current_day_bar_is_dropped():
    """A synthetic flat wall-clock bar is not an official candle."""

    frame = fetcher.normalize_response_data(
        _session_payload(["2026-09-15 15:29:00", "2026-09-15 18:44:00"])
    )

    assert [str(value) for value in frame["timestamp"]] == ["2026-09-15 15:29:00"]


def test_a_chunk_entirely_outside_the_session_is_empty_not_an_error():
    """An empty result is a holiday-shaped answer, not a failure."""

    frame = fetcher.normalize_response_data(_session_payload(["2026-09-15 18:44:00"]))

    assert frame.empty


def test_the_session_window_is_market_data_healths_own():
    """The extractor and the runner must agree on what a session is."""

    start, end = fetcher.MARKET_SESSION_START, fetcher.MARKET_SESSION_END
    assert (start.hour, start.minute) == (9, 15)
    assert (end.hour, end.minute) == (15, 30)


def _minute_candles(count, start="2022-03-25 09:15:00"):
    """`count` identical, perfectly well-formed one-minute candles."""

    base = pd.Timestamp(start)
    return pd.DataFrame(
        {
            "timestamp": [base + pd.Timedelta(minutes=index) for index in range(count)],
            "open": [100.0] * count,
            "high": [101.0] * count,
            "low": [99.0] * count,
            "close": [100.5] * count,
            "volume": [0.0] * count,
        }
    )


def test_a_stray_self_contradicting_candle_is_dropped_and_named(capsys):
    """The real one that blocked the backfill: an open ABOVE its own high.

    Measured at 2022-03-25 09:15 -- open 17289.00, high 17287.10 -- one row in
    23,380. No reading of a candle makes that right, so it goes; failing five
    years of history on it would be the wrong trade.
    """

    frame = _minute_candles(2000)
    frame.loc[0, ["open", "high", "low", "close"]] = [17289.0, 17287.0996, 17264.85, 17266.30]

    kept = fetcher.drop_impossible_candles(frame)

    assert len(kept) == 1999
    assert pd.Timestamp("2022-03-25 09:15:00") not in set(kept["timestamp"])
    assert "2022-03-25 09:15:00" in capsys.readouterr().out, "a dropped bar must be named"


def test_a_chunk_that_is_mostly_impossible_still_fails():
    """Many bad candles is not noise -- it says this is not the series we asked for."""

    frame = _minute_candles(10)
    frame.loc[0, "high"] = 1.0

    with pytest.raises(fetcher.MarketDataValidationError, match="too many to be stray"):
        fetcher.drop_impossible_candles(frame)


def test_every_shape_of_impossible_candle_is_caught():
    for column, value in [("high", 1.0), ("low", 1000.0)]:
        frame = _minute_candles(2000)
        frame.loc[0, column] = value
        assert len(fetcher.drop_impossible_candles(frame)) == 1999, column

    crossed = _minute_candles(2000)
    crossed.loc[0, ["open", "high", "low", "close"]] = [100.0, 99.0, 101.0, 100.0]
    assert len(fetcher.drop_impossible_candles(crossed)) == 1999


def test_a_clean_chunk_is_passed_through_untouched():
    frame = _minute_candles(50)

    kept = fetcher.drop_impossible_candles(frame)

    assert len(kept) == 50
    assert kept.equals(frame)


def test_a_missing_volume_cell_is_zero_for_an_index():
    """An index has no traded volume.

    An absent volume COLUMN is already treated as zero, so an absent cell is
    handled consistently.
    """

    payload = _session_payload(["2026-09-15 09:15:00", "2026-09-15 09:16:00"])
    payload["volume"] = [float("nan"), 5.0]

    frame = fetcher.normalize_response_data(payload, instrument_type="INDEX")

    assert list(frame["volume"]) == [0.0, 5.0]


def test_a_missing_volume_cell_is_still_refused_off_an_index():
    """The forgiveness is scoped to instruments that have no volume to give.

    An earlier version filled NaN in before the validity test, which quietly
    exempted EVERY instrument -- including the equity and F&O segments this
    engine also serves, where an absent volume is corruption rather than a
    non-answer.
    """

    payload = _session_payload(["2026-09-15 09:15:00", "2026-09-15 09:16:00"])
    payload["volume"] = [float("nan"), 5.0]

    with pytest.raises(fetcher.MarketDataValidationError, match="invalid volume"):
        fetcher.normalize_response_data(payload, instrument_type="EQUITY")


def test_a_negative_or_infinite_volume_is_still_refused():
    """Absent is not the same as corrupt."""

    for bad in (-1.0, float("inf")):
        payload = _session_payload(["2026-09-15 09:15:00", "2026-09-15 09:16:00"])
        payload["volume"] = [bad, 5.0]
        with pytest.raises(fetcher.MarketDataValidationError, match="invalid volume"):
            fetcher.normalize_response_data(payload)


def test_weekend_rows_are_dropped():
    """The older windows carry Saturday rows whose prices are not the index.

    Measured on Saturday 2022-04-09: the close jumps 18396 -> 18584 -> 18371 ->
    18842 inside one hour. They sit INSIDE session hours, so a time-of-day clip
    alone lets them through.
    """

    frame = fetcher.normalize_response_data(
        _session_payload([
            "2026-09-11 09:15:00",   # Friday
            "2026-09-12 11:30:00",   # Saturday, mid-session by the clock
            "2026-09-13 11:30:00",   # Sunday
            "2026-09-14 09:15:00",   # Monday
        ])
    )

    kept = [str(value) for value in frame["timestamp"]]
    assert kept == ["2026-09-11 09:15:00", "2026-09-14 09:15:00"]


def test_an_index_bar_with_negative_volume_keeps_its_price(capsys):
    """An index has no volume, so the field is zeroed rather than the bar dropped.

    Invalid volume is not a reason to discard otherwise valid price bars.
    """

    payload = _session_payload(["2026-09-15 09:15:00", "2026-09-15 09:16:00"])
    payload["volume"] = [-2.0, 5.0]

    frame = fetcher.normalize_response_data(payload, instrument_type="INDEX")

    assert len(frame) == 2, "the price bar survives"
    assert list(frame["volume"]) == [0.0, 5.0]
    assert "Zeroing 1" in capsys.readouterr().out


def test_a_non_index_with_negative_volume_is_still_refused():
    """Where volume is a real quantity, a negative one is corruption."""

    payload = _session_payload(["2026-09-15 09:15:00", "2026-09-15 09:16:00"])
    payload["volume"] = [-2.0, 5.0]

    with pytest.raises(fetcher.MarketDataValidationError, match="invalid volume"):
        fetcher.normalize_response_data(payload, instrument_type="EQUITY")


def test_an_unparseable_timestamp_refuses_the_chunk():
    """A row that will not parse must fail loudly, not vanish.

    `errors="coerce"` turns it into NaT, and NaT compares False against a
    `datetime.time`, so the session clip would silently DROP it -- the chunk
    comes back quietly short, is appended, and advances the resume point past a
    gap nobody was told about.
    """

    payload = _session_payload(["2026-09-15 09:15:00", "2026-09-15 09:16:00"])
    payload["timestamp"] = [payload["timestamp"][0], "not-a-timestamp"]

    with pytest.raises(fetcher.MarketDataValidationError, match="unparseable timestamp"):
        fetcher.normalize_response_data(payload, instrument_type="INDEX")
