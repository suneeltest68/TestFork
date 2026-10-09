package ema

import java.time.LocalDateTime
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull

class EmaTrendStrategyTest {
    private fun candle(i: Int, open: Double, high: Double, low: Double, close: Double) =
        Candle(LocalDateTime.of(2026, 1, 1, 9, 15).plusMinutes(i.toLong()), open, high, low, close)

    @Test fun indicatorsAreEmptyForNoCandles() {
        assertEquals(emptyList(), EmaTrendStrategy().indicators(emptyList()))
    }

    @Test fun exitLongWhenLowBreachesEma11() {
        val trend = (0 until 70).map { i ->
            val c = 100.0 + i
            candle(i, c - 0.2, c + 0.5, c - 0.5, c + 0.3)
        }.toMutableList()
        val last = trend.last()
        trend[trend.lastIndex] = last.copy(low = 1.0)
        val decision = EmaTrendStrategy().evaluate(trend, PositionContext(Direction.LONG))
        assertEquals(Action.EXIT, decision.action)
        assertEquals("EMA11_EXIT", decision.exitReason)
    }

    @Test fun exitShortWhenHighBreachesEma11() {
        val trend = (0 until 70).map { i ->
            val c = 200.0 - i
            candle(i, c + 0.2, c + 0.5, c - 0.5, c - 0.3)
        }.toMutableList()
        val last = trend.last()
        trend[trend.lastIndex] = last.copy(high = 1000.0)
        val decision = EmaTrendStrategy().evaluate(trend, PositionContext(Direction.SHORT))
        assertEquals(Action.EXIT, decision.action)
        assertNotNull(decision.timestamp)
    }
}
