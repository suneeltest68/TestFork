# Index history extractors

These scripts download historical OHLC candles for NIFTY, BANKNIFTY, and
FINNIFTY through the Fyers API. They preserve chunked downloads, candle
validation, and atomic resume manifests so an interrupted run can continue
without discarding completed history.

## Scripts

- `nifty_1m_5y_data_fetch_fyers.py` — NIFTY (runner identity 13).
- `banknifty_1m_5y_data_fetch_fyers.py` — BANKNIFTY (runner identity 25).
- `finnifty_1m_5y_data_fetch_fyers.py` — FINNIFTY (runner identity 27).
- `index_1m_5y_data_fetch_fyers_common.py` — shared engine; call a wrapper
  instead of running this module directly.

For example:

```sh
python "Data Extractors/nifty_1m_5y_data_fetch_fyers.py" --lookback 5y
```

The unified CLI is also available:

```sh
python algo.py fetch-data --index nifty --interval 1 --lookback 5y
```

Each wrapper defaults to a CSV in `Backtest Outputs/`. Use `--start-date`,
`--end-date`, `--output`, or `--chunk-days` to customize the request. Pass
`--help` for the full list. A sibling `.manifest.json` records completed
chunks; rerunning the same command resumes safely. `--no-resume` forces a
fresh atomic rebuild.

## Credentials

Set `FYERS_CLIENT_ID` and `FYERS_ACCESS_TOKEN` in `Dependencies/.env`. The
access token is environment-only and has no CLI flag so it cannot land in
shell history. Dhan credentials are not used by these extractors; they remain
available separately for Dhan order execution.

The expired-options downloader has been retired. Existing local option-history
CSVs can still be used by backtests that consume them.
