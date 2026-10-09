# EMA Trend Strategy — Kotlin

This branch is a clean Kotlin-only extraction of the repository's **EMA Trend** strategy. The original Python trading system, other strategies, broker adapters, data extractors, and unrelated files are intentionally not included in this branch.

## Strategy preserved

- EMA periods: 4, 11, 18
- ATR / ADX periods: 14 / 14
- Slope lookback: 3 candles
- ADX threshold: greater than 20
- EMA distance filter: 0.5 × ATR
- EMA11 slope threshold: 0.3 × ATR
- EMA18 slope threshold: 0.2 × ATR
- Candle body must be at least 50% of candle range
- Long entries require bullish EMA ordering and positive, strengthening slopes
- Short entries require bearish EMA ordering and negative, strengthening slopes
- Exit a long when candle low breaches EMA11; exit a short when candle high breaches EMA11

Indicator warm-up uses a deterministic Kotlin implementation of EMA, Wilder ATR, and Wilder ADX. This removes Python, pandas, NumPy, and TA-Lib runtime dependencies. Small numerical differences from TA-Lib may exist and should be compared against reference candles before live use.

## Build and run

Requires JDK 17+ and Gradle (or use the Gradle wrapper if added).

```bash
gradle test
gradle run
```

The sample application evaluates a small synthetic candle series and prints the latest EMA decision. It does **not** connect to a broker or place orders.

## Input format

Create `Candle(timestamp, open, high, low, close)` records in chronological order. `EmaTrendStrategy.evaluate(candles, position)` returns `HOLD`, `ENTER_LONG`, `ENTER_SHORT`, or `EXIT`.

## Safety

This project is signal logic only. It deliberately has no credentials, broker integration, order placement, or live-trading switches. Validate indicator parity, entry/exit behavior, transaction costs, and paper-trading results before adding execution.
