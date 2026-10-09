# FYERS adapter

This directory contains the FYERS v3 implementation for the shared multi-strategy
runner. The runner continues to use the existing strategy code, shared market-data
store, execution ledger, exposure audit, and broker-neutral order contract.

## Configure

1. Create a FYERS API app and set its redirect URI to the value in
   `Dependencies/.env` (example: `http://127.0.0.1:8080/`).
2. Set `FYERS_APP_ID`, `FYERS_API_SECRET`, and `FYERS_REDIRECT_URI` in the
   **local, untracked** `Dependencies/.env`.
3. Run `python algo.py setup-token` outside market hours. The script stores the
   access token locally and does not print the token.
4. Install runtime dependencies with `python -m pip install -r requirements.txt`.
5. Set `LIVE_BROKER=FYERS` and `MARKET_DATA_SOURCE=WEBSOCKET`. Paper trading
   remains the default: `LIVE_TRADING_ENABLED=false`.
6. Run the master and validate the live prices and minute candles in paper mode
   before considering live execution.

The FYERS adapter reads the public FYERS NSE_FO/NSE_CM symbol masters and maps
the runner's existing internal contract metadata to exact FYERS symbols. If
`FYERS_LEGACY_INSTRUMENT_CSV` is blank, it searches for the newest
`Dependencies/all_instrument*.csv` file. This file is metadata only; live
prices, candles, quotes, option-chain data, and orders use FYERS APIs.

## Live-order safety

FYERS live orders are separately blocked unless
`FYERS_LIVE_COMPLIANCE_CONFIRMED=true`. Do not set this just to make the error
go away: first confirm with FYERS that the API app is eligible for trading and
that its required static IP is whitelisted. This is an operator acknowledgement,
not an automatic compliance check. The repository's global live flag, per-strategy
live flags, startup order/position reconciliation, and risk checks still apply.

The execution adapter treats ambiguous order submissions as UNKNOWN and blocks
further submissions until reconciliation. An acknowledgement is not a fill.
Do not use live credentials to run unit tests.

## Diagnostic

`python algo.py diagnose --broker fyers CE 26000 2026-10-27` checks the profile
and resolves the requested option from the FYERS master. The diagnostic is
read-only and never submits an order.
