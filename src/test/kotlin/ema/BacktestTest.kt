package ema

import java.time.LocalDateTime
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class BacktestTest {
    private fun candle(minute: Int, open: Double, high: Double, low: Double, close: Double) =
        Candle(LocalDateTime.of(2026, 1, 1, 9, 15).plusMinutes(minute.toLong()), open, high, low, close)

    @Test fun resampleProducesFiveMinuteOhlc() {
        val candles = (0 until 10).map { i -> candle(i, 100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i) }
        val bars = Backtest.resample(candles, 5)
        assertEquals(2, bars.size)
        assertEquals(100.0, bars[0].open)
        assertEquals(106.0, bars[0].high)
        assertEquals(99.0, bars[0].low)
        assertEquals(105.0, bars[0].close)
    }

    @Test fun csvReaderRequiresCandleColumns() {
        val path = java.nio.file.Files.createTempFile("ema", ".csv")
        try {
            java.nio.file.Files.writeString(path, "timestamp,open,high,low,close\n2026-01-01T09:15:00,100,102,99,101\n")
            assertEquals(1, Backtest.readCsv(path).size)
        } finally { java.nio.file.Files.deleteIfExists(path) }
    }

    @Test fun reportHasZeroProfitFactorWhenNoLossesAndNoWins() {
        val report = BacktestReport(emptyList())
        assertTrue(report.netPnl == 0.0)
        assertEquals(0, report.trades.size)
    }
}
