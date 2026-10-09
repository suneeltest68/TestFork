# EMA Trend Strategy — Kotlin + FYERS

A focused Kotlin project for **EMA Trend backtesting and paper trading** using FYERS data. Other strategies and the original Python multi-strategy framework are intentionally excluded. This project does not place real orders.

## Features

- FYERS REST historical candles for warm-up and historical backtests
- FYERS WebSocket live LTP stream for paper-mode price updates
- EMA 4/11/18, ATR(14), ADX(14), 3-bar slope filters, EMA-distance and candle-body confirmation
- Five-minute strategy candles derived from one-minute OHLC
- CSV backtesting without broker credentials
- Paper position tracking and underlying-point P&L, without order submission

## Setup

Requires JDK 17+ and Gradle.

Create a local `.env` file in the project root (do not commit it):

```dotenv
FYERS_APP_ID=your_app_id
FYERS_ACCESS_TOKEN=your_access_token
FYERS_SYMBOL=NSE:NIFTY50-INDEX
```

The FYERS access token is short-lived and must be refreshed using your approved FYERS OAuth flow. Never commit tokens or API secrets. The app ID and token are sent to FYERS using its documented authorization header.

## Run a backtest

From a CSV with columns `timestamp,open,high,low,close`:

```bash
gradle test
gradle run --args='backtest --csv data/nifty_1m.csv'
```

Or fetch historical candles directly from FYERS:

```bash
gradle run --args='backtest --fyers --from 2026-01-01 --to 2026-01-31'
```

Historical data is resampled to five-minute candles before EMA signal evaluation. Backtest P&L is measured in underlying index points, not option-premium P&L. Brokerage, slippage, taxes, option selection, lot sizing and realistic option fills are not modeled.

## Run paper trading

```bash
gradle run --args='paper'
```

Paper mode warms indicators from FYERS REST history, then subscribes to FYERS WebSocket prices. It logs simulated signal entries/exits only. It cannot send orders; there is no execution client, live-trading switch, or order-placement API in this project.

## Safety and limitations

- Start with paper mode only.
- Confirm the FYERS WebSocket auth/subscription payload and REST response schema against your current FYERS API app.
- Check candle timestamps and the five-minute bucket boundary against reference data.
- Paper P&L is an underlying-price proxy; the original strategy's option contract and premium-level results require a separate option simulator.
- This Kotlin port must be compared against the original Python/TA-Lib output before relying on its signals.
