from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd
import pytest

from Dependencies.ema_options_backtest import (
    OptionContract,
    SignalEvent,
    TradeRecord,
    _environment_float,
    _environment_int,
    _expired_option_symbol_suffix,
    _option_candles_as_ist,
    _write_outputs,
    load_fyers_expired_option_contracts,
    replay_option_trades,
    resample_complete_ohlc,
    select_atm_contract,
)


@pytest.mark.parametrize("raw_value", ["1.5", "inf", "nan"])
def test_environment_int_rejects_non_whole_or_non_finite_values(monkeypatch, raw_value):
    monkeypatch.setenv("EMA_LOTS", raw_value)

    with pytest.raises(ValueError, match="EMA_LOTS must be a whole number"):
        _environment_int("EMA_LOTS", 1)


@pytest.mark.parametrize("raw_value", ["inf", "-inf", "nan"])
def test_environment_float_rejects_non_finite_values(monkeypatch, raw_value):
    monkeypatch.setenv("BROKERAGE", raw_value)

    with pytest.raises(ValueError, match="BROKERAGE must be finite"):
        _environment_float("BROKERAGE", 80.0)


def _contract(expiry: date, strike: float, right: str, symbol: str) -> OptionContract:
    return OptionContract(symbol, expiry, strike, right, 65)


def _history_frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    timestamps = pd.to_datetime([timestamp for timestamp, _ in rows]).tz_localize("Asia/Kolkata")
    return pd.DataFrame(
        {
            "timestamp": [int(timestamp.timestamp()) for timestamp in timestamps],
            "open": [price for _, price in rows],
            "high": [price for _, price in rows],
            "low": [price for _, price in rows],
            "close": [price for _, price in rows],
            "volume": [0] * len(rows),
        }
    )


def test_resample_complete_ohlc_keeps_only_full_five_minute_buckets():
    rows = [
        ("2026-10-09 09:25", 1, 3, 1, 2),
        ("2026-10-09 09:26", 2, 4, 2, 3),
        ("2026-10-09 09:27", 3, 5, 2, 4),
        ("2026-10-09 09:28", 4, 6, 3, 5),
        ("2026-10-09 09:29", 5, 7, 4, 6),
        ("2026-10-09 09:30", 6, 8, 5, 7),
        ("2026-10-09 09:31", 7, 9, 6, 8),
        ("2026-10-09 09:32", 8, 10, 7, 9),
        ("2026-10-09 09:33", 9, 11, 8, 10),
    ]
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])

    result = resample_complete_ohlc(frame)

    assert len(result) == 1
    assert result.iloc[0].to_dict() == {
        "timestamp": pd.Timestamp("2026-10-09 09:25:00"),
        "open": 1.0,
        "high": 7.0,
        "low": 1.0,
        "close": 6.0,
    }


def test_atm_selection_uses_second_future_expiry_and_direction():
    contracts = [
        _contract(date(2026, 10, 13), 22300, "CE", "near-ce"),
        _contract(date(2026, 10, 19), 22300, "CE", "second-ce"),
        _contract(date(2026, 10, 19), 22300, "PE", "second-pe"),
        _contract(date(2026, 10, 19), 22250, "CE", "lower-ce"),
        _contract(date(2026, 10, 27), 22300, "PE", "later-pe"),
    ]

    assert select_atm_contract(contracts, date(2026, 10, 9), 22301, "LONG").symbol == "second-ce"
    assert select_atm_contract(contracts, date(2026, 10, 9), 22301, "SHORT").symbol == "second-pe"


def test_atm_selection_fails_when_second_expiry_is_not_in_master():
    with pytest.raises(ValueError, match="Need two Fyers NIFTY expiries"):
        select_atm_contract(
            [_contract(date(2026, 10, 19), 22300, "CE", "only-contract")],
            date(2026, 10, 9),
            22300,
            "LONG",
        )


def test_expired_option_symbol_parser_handles_fyers_abbreviated_month_weekly_symbol():
    match = _expired_option_symbol_suffix("NSE:NIFTY26O0617800PE", date(2026, 10, 6))

    assert match is not None
    assert match.group("strike") == "17800"
    assert match.group("right") == "PE"


def test_expired_contracts_are_loaded_for_each_available_historical_expiry():
    class _ExpiredFyers:
        def __init__(self):
            self.requested_expiries = []

        def fetch_expiry_dates(self, symbol, start_date, end_date):
            assert symbol == "NSE:NIFTY50-INDEX"
            assert start_date == date(2026, 5, 1)
            assert end_date == date(2026, 7, 15)
            return [
                date(2026, 5, 7),
                date(2026, 5, 14),
                date(2026, 5, 21),
                date(2026, 7, 16),
            ]

        def fetch_expired_option_symbols(self, symbol, expiry):
            assert symbol == "NSE:NIFTY50-INDEX"
            self.requested_expiries.append(expiry)
            return [
                f"NSE:NIFTY26{expiry.month}{expiry.day:02d}22000CE",
                f"NSE:NIFTY26{expiry.month}{expiry.day:02d}22000PE",
            ]

    client = _ExpiredFyers()

    contracts = load_fyers_expired_option_contracts(
        client,
        date(2026, 5, 1),
        date(2026, 5, 31),
    )

    assert client.requested_expiries == [
        date(2026, 5, 7),
        date(2026, 5, 14),
        date(2026, 5, 21),
    ]
    assert contracts[0] == OptionContract("NSE:NIFTY2650722000CE", date(2026, 5, 7), 22000, "CE", 65)
    assert select_atm_contract(contracts, date(2026, 5, 1), 22000, "LONG").expiry == date(2026, 5, 14)


def test_replay_fetches_option_after_entry_and_uses_first_five_second_open():
    contract = _contract(date(2026, 10, 19), 22300, "PE", "NSE:NIFTY26O1922300PE")
    earlier_expiry = _contract(date(2026, 10, 13), 22300, "PE", "NSE:NIFTY26O1322300PE")
    events = [
        SignalEvent(
            "ENTRY",
            datetime(2026, 10, 9, 9, 25),
            datetime(2026, 10, 9, 9, 30),
            "SHORT",
            22300,
            "EMA entry signal",
        ),
        SignalEvent(
            "EXIT",
            datetime(2026, 10, 9, 10, 0),
            datetime(2026, 10, 9, 10, 5),
            "SHORT",
            22200,
            "EMA11_EXIT",
        ),
    ]
    fetch_calls: list[tuple[str, date]] = []

    def fetch_history(symbol: str, trading_date: date) -> pd.DataFrame:
        fetch_calls.append((symbol, trading_date))
        return _history_frame(
            [
                ("2026-10-09 09:29:55", 9),
                ("2026-10-09 09:30:00", 10),
                ("2026-10-09 10:04:55", 11),
                ("2026-10-09 10:05:00", 12),
            ]
        )

    trades, open_trade = replay_option_trades(
        events,
        contracts=[earlier_expiry, contract],
        lots=1,
        brokerage_per_trade=80,
        fetch_option_history=fetch_history,
    )

    assert fetch_calls == [(contract.symbol, date(2026, 10, 9))]
    assert len(trades) == 1
    assert trades[0].entry_fill_time == datetime(2026, 10, 9, 9, 30)
    assert trades[0].entry_price == 10
    assert trades[0].exit_fill_time == datetime(2026, 10, 9, 10, 5)
    assert trades[0].exit_price == 12
    assert trades[0].gross_pnl == 130
    assert trades[0].net_pnl_estimate == 50
    assert open_trade is None


def test_replay_fails_if_the_required_option_fill_candle_is_missing():
    contract = _contract(date(2026, 10, 19), 22300, "CE", "NSE:NIFTY26O1922300CE")
    earlier_expiry = _contract(date(2026, 10, 13), 22300, "CE", "NSE:NIFTY26O1322300CE")
    event = SignalEvent(
        "ENTRY",
        datetime(2026, 10, 9, 15, 10),
        datetime(2026, 10, 9, 15, 15),
        "LONG",
        22300,
        "EMA entry signal",
    )

    with pytest.raises(RuntimeError, match="No option candle"):
        replay_option_trades(
            [event],
            contracts=[earlier_expiry, contract],
            lots=1,
            brokerage_per_trade=80,
            fetch_option_history=lambda _symbol, _day: _history_frame([("2026-10-09 15:14:55", 10)]),
        )


def test_option_history_is_filtered_to_regular_market_session():
    frame = _history_frame(
        [
            ("2026-10-09 09:14:55", 10),
            ("2026-10-09 09:15:00", 11),
            ("2026-10-09 15:29:55", 12),
            ("2026-10-09 15:30:00", 13),
        ]
    )

    result = _option_candles_as_ist(frame, date(2026, 10, 9))

    assert result["open"].tolist() == [11, 12]


def test_outputs_keep_trade_csv_headers_when_no_entries(tmp_path):
    trades_path, summary_path = _write_outputs(
        [],
        None,
        output_dir=tmp_path,
        start_date=date(2026, 10, 9),
        end_date=date(2026, 10, 9),
        lots=1,
        timeframe_minutes=5,
        brokerage_per_trade=80,
    )

    assert pd.read_csv(trades_path).empty
    summary = pd.read_json(summary_path, typ="series")
    assert summary["closed_trades"] == 0
    assert "asynchronous" in summary["risk_note"]


def test_outputs_include_cumulative_pnl_drawdown_and_html_trade_report(tmp_path):
    def trade(entry_minute: int, exit_minute: int, net_pnl: float) -> TradeRecord:
        return TradeRecord(
            direction="LONG",
            symbol="NSE:NIFTY26O1922400CE",
            right="CE",
            strike=22400,
            expiry=date(2026, 10, 19),
            lots=1,
            lot_size=65,
            quantity=65,
            entry_signal_time=datetime(2026, 10, 9, 9, 25),
            entry_fill_time=datetime(2026, 10, 9, 9, entry_minute),
            entry_spot_close=22375,
            entry_price=100,
            exit_signal_time=datetime(2026, 10, 9, 10, 0),
            exit_fill_time=datetime(2026, 10, 9, 10, exit_minute),
            exit_price=100 + net_pnl / 65,
            exit_reason="EMA11_EXIT",
            gross_pnl=net_pnl + 80,
            brokerage_estimate=80,
            net_pnl_estimate=net_pnl,
        )

    trades_path, summary_path = _write_outputs(
        [trade(30, 35, 100), trade(0, 5, -150)],
        None,
        output_dir=tmp_path,
        start_date=date(2026, 10, 9),
        end_date=date(2026, 10, 9),
        lots=1,
        timeframe_minutes=5,
        brokerage_per_trade=80,
    )

    rows = pd.read_csv(trades_path)
    assert rows["cumulative_pnl"].tolist() == [100, -50]
    assert rows["drawdown_from_peak"].tolist() == [0, -150]
    assert rows["max_drawdown_to_date"].tolist() == [0, 150]

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["max_drawdown"] == 150
    report = (tmp_path / "ema_options_2026-10-09_to_2026-10-09_report.html").read_text(encoding="utf-8")
    assert "Entry time" in report
    assert "Exit time" in report
    assert "Option used" in report
    assert "Expiry date" in report
    assert "Cumulative P&amp;L" in report
    assert "-₹150.00" in report
