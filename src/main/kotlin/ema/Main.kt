package ema

import java.time.LocalDateTime

fun main() {
    val candles = (0 until 80).map { i ->
        val base = 22000.0 + i * 2.0
        Candle(
            timestamp = LocalDateTime.of(2026, 1, 1, 9, 15).plusMinutes(i.toLong()),
            open = base - 0.5,
            high = base + 1.5,
            low = base - 1.0,
            close = base + 1.0
        )
    }
    val strategy = EmaTrendStrategy()
    val latest = strategy.indicators(candles).lastOrNull()
    println("EMA Trend signal-only demo (no broker connection)")
    println("Latest indicators: EMA4=${latest?.ema4}, EMA11=${latest?.ema11}, EMA18=${latest?.ema18}, ATR=${latest?.atr}, ADX=${latest?.adx}")
    println("Decision: ${strategy.evaluate(candles).action}")
}
