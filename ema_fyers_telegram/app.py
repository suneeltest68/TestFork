"""FYERS market-data -> unchanged EMA signal engine -> Telegram alerts.

This service does not place, modify, or cancel broker orders. It polls FYERS
historical candles, evaluates only the latest completed candle, and sends
entry/exit signal notifications to Telegram.
"""
from __future__ import annotations

import os
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

from ema_trend_strategy_logic import (
    EMATrendPositionContext,
    EMATrendSignalEngine,
    build_ema_trend_with_indicators,
)

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ema-fyers-telegram")
IST = ZoneInfo("Asia/Kolkata")
FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_candles(session: requests.Session, symbol: str, resolution: str, days: int) -> pd.DataFrame:
    token = required_env("FYERS_ACCESS_TOKEN")
    app_id = required_env("FYERS_APP_ID")
    now = datetime.now(IST)
    start = now - timedelta(days=days)
    response = session.get(
        FYERS_HISTORY_URL,
        params={
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start.strftime("%Y-%m-%d"),
            "range_to": now.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        },
        headers={"Authorization": f"{app_id}:{token}"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("s") != "ok" or not payload.get("candles"):
        raise RuntimeError(f"FYERS history response was not OK: {payload.get('message', payload.get('s'))}")
    frame = pd.DataFrame(payload["candles"], columns=["epoch", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame.pop("epoch"), unit="s", utc=True).dt.tz_convert(IST)
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
    return frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def send_telegram(session: requests.Session, text: str) -> None:
    if os.getenv("TELEGRAM_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        log.info("Telegram disabled; signal: %s", text.replace("\n", " | "))
        return
    token = required_env("TELEGRAM_BOT_TOKEN")
    chat_id = required_env("TELEGRAM_CHAT_ID")
    response = session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=15,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram send failed: {result}")


def evaluate_latest(frame: pd.DataFrame, engine: EMATrendSignalEngine,
                    position: EMATrendPositionContext | None):
    enriched = build_ema_trend_with_indicators(frame)
    if enriched.empty:
        return None, position, None
    candle = enriched.iloc[-1]
    decision = engine.evaluate_candle(enriched, position_context=position)
    action = getattr(decision, "action", "HOLD")
    if action == "ENTER_LONG":
        position = EMATrendPositionContext(direction="LONG", entry_underlying=float(candle["close"]))
    elif action == "ENTER_SHORT":
        position = EMATrendPositionContext(direction="SHORT", entry_underlying=float(candle["close"]))
    elif action == "EXIT":
        position = None
    return decision, position, candle


def format_alert(action: str, symbol: str, candle: pd.Series, decision) -> str:
    stamp = candle["timestamp"].strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        f"EMA Trend Signal: {action}",
        f"Symbol: {symbol}",
        f"Timeframe: {os.getenv('FYERS_RESOLUTION', '5')} minute",
        f"Candle: {stamp}",
        f"Close: {float(candle['close']):.2f}",
    ]
    reason = getattr(decision, "exit_reason", "")
    if reason:
        lines.append(f"Reason: {reason}")
    lines.append("Signal only — no broker order was placed.")
    return "\n".join(lines)


def run() -> None:
    symbol = required_env("FYERS_SYMBOL")
    resolution = os.getenv("FYERS_RESOLUTION", "5")
    poll_seconds = max(15, int(os.getenv("POLL_SECONDS", "30")))
    history_days = max(3, int(os.getenv("HISTORY_DAYS", "10")))
    session = requests.Session()
    engine = EMATrendSignalEngine()
    position = None
    last_processed = None
    log.info("Starting EMA notifier for %s at %s-minute resolution (signal-only)", symbol, resolution)

    while True:
        try:
            candles = fetch_candles(session, symbol, resolution, history_days)
            if len(candles) < 120:
                log.warning("Waiting for indicator warm-up; only %d candles available", len(candles))
                time.sleep(poll_seconds)
                continue
            # Do not process the currently-forming candle. FYERS history may
            # include it, so conservatively drop the latest row until its interval ends.
            now = pd.Timestamp.now(tz=IST)
            interval = pd.Timedelta(minutes=int(resolution))
            candles = candles[candles["timestamp"] + interval <= now]
            if candles.empty:
                time.sleep(poll_seconds)
                continue
            stamp = candles.iloc[-1]["timestamp"]
            if last_processed is not None and stamp <= last_processed:
                time.sleep(poll_seconds)
                continue

            # Rebuild a fresh engine for deterministic single-candle evaluation
            # against the unchanged strategy's full historical context.
            engine = EMATrendSignalEngine()
            enriched = build_ema_trend_with_indicators(candles)
            decision = engine.evaluate_candle(enriched, position_context=position)
            action = getattr(decision, "action", "HOLD")
            candle = enriched.iloc[-1]
            if action == "ENTER_LONG":
                position = EMATrendPositionContext(direction="LONG", entry_underlying=float(candle["close"]))
            elif action == "ENTER_SHORT":
                position = EMATrendPositionContext(direction="SHORT", entry_underlying=float(candle["close"]))
            elif action == "EXIT":
                position = None
            if action in {"ENTER_LONG", "ENTER_SHORT", "EXIT"}:
                message = format_alert(action, symbol, candle, decision)
                send_telegram(session, message)
                log.info("Signal sent: %s at %s", action, stamp)
            else:
                log.info("No signal on completed candle %s", stamp)
            last_processed = stamp
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            log.exception("Polling/evaluation failed: %s", exc)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    run()
