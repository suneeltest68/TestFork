# EMA Trend + FYERS + Telegram (Python)

A focused Python service for the repository's EMA Trend strategy. It fetches OHLC candles from the FYERS history API, runs the EMA strategy engine, and posts entry/exit signal alerts to Telegram. It deliberately contains **no order-placement code**.

## Strategy integrity

`ema_trend_strategy_logic.py` is copied byte-for-byte from `Signal Generators/ema_trend_strategy_logic.py` on `main`. Do not edit the strategy logic as part of the broker/notification integration. This standalone folder contains only the EMA strategy engine and the adapter/runtime files needed for this use case.

## Setup

Python 3.11+ recommended. From this directory:

```bash
python -m venv .venv
source .venv/bin/activate
# Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with FYERS app ID/access token, a FYERS-supported symbol, and Telegram bot token/chat ID. Keep `.env` private; never commit access tokens.

## Run

```bash
python app.py
```

The process polls every `POLL_SECONDS`, fetches recent history, ignores the current incomplete candle, and evaluates the latest completed candle. Keep the process running on a reliable VPS/server for deployment. FYERS access tokens expire and must be refreshed according to the authentication flow for your account.

## Telegram setup

1. Create a bot using Telegram's `@BotFather`.
2. Add the bot to the destination chat/channel and grant posting permission where required.
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

## Safety and limitations

- Signal notifications only: this service never submits, modifies, or cancels orders.
- Confirm the correct FYERS API base URL, app ID/token format, symbol and history response with your FYERS account before production deployment; the endpoint/authentication details may vary by API version.
- The strategy is evaluated only on completed candles. Validate the signal engine's state/position lifecycle with historical replay before relying on repeated entry/exit notifications.
- This initial integration has not been run against a live FYERS account or Telegram bot, and tests have not yet been executed.
- It is not a complete historical backtesting/reporting CLI; it is a live signal notifier. Historical backtest support can be added separately without changing the strategy logic.
- No profit, fill, or signal delivery is guaranteed. Monitor logs and use paper/signal-only validation before making any trading decisions.
