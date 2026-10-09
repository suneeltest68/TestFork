package ema

import java.time.LocalDateTime
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

data class Candle(
    val timestamp: LocalDateTime,
    val open: Double,
    val high: Double,
    val low: Double,
    val close: Double
) {
    init {
        require(listOf(open, high, low, close).all(Double::isFinite)) { "OHLC prices must be finite" }
        require(high >= max(open, max(close, low))) { "high must be the maximum OHLC price" }
        require(low <= min(open, min(close, high))) { "low must be the minimum OHLC price" }
    }
}

data class EmaConfig(
    val emaFastPeriod: Int = 4,
    val emaMidPeriod: Int = 11,
    val emaSlowPeriod: Int = 18,
    val atrPeriod: Int = 14,
    val adxPeriod: Int = 14,
    val slopeLookback: Int = 3,
    val adxThreshold: Double = 20.0,
    val distanceAtrMultiplier: Double = 0.5,
    val ema11SlopeAtrMultiplier: Double = 0.3,
    val ema18SlopeAtrMultiplier: Double = 0.2,
    val fullBodyMinRatio: Double = 0.5
) {
    init {
        require(emaFastPeriod > 0 && emaMidPeriod > 0 && emaSlowPeriod > 0)
        require(emaFastPeriod < emaMidPeriod && emaMidPeriod < emaSlowPeriod)
        require(atrPeriod > 0 && adxPeriod > 0 && slopeLookback > 0)
        require(adxThreshold >= 0 && distanceAtrMultiplier >= 0)
        require(ema11SlopeAtrMultiplier >= 0 && ema18SlopeAtrMultiplier >= 0)
        require(fullBodyMinRatio in 0.0..1.0)
    }
}

data class PositionContext(val direction: Direction)
enum class Direction { LONG, SHORT }
enum class Action { HOLD, ENTER_LONG, ENTER_SHORT, EXIT }

data class Decision(
    val action: Action = Action.HOLD,
    val exitReason: String? = null,
    val timestamp: LocalDateTime? = null
)

data class IndicatorCandle(
    val candle: Candle,
    val ema4: Double?,
    val ema11: Double?,
    val ema18: Double?,
    val atr: Double?,
    val adx: Double?,
    val ema11Slope: Double?,
    val ema18Slope: Double?,
    val ema11SlopeStrength: Double?,
    val ema18SlopeStrength: Double?,
    val ema11DeltaCurrent: Double?,
    val ema11DeltaPrevious: Double?,
    val longSetup: Boolean,
    val shortSetup: Boolean
)

class EmaTrendStrategy(private val config: EmaConfig = EmaConfig()) {
    fun indicators(input: List<Candle>): List<IndicatorCandle> {
        val candles = input.sortedBy { it.timestamp }.distinctBy { it.timestamp }
        if (candles.isEmpty()) return emptyList()

        val closes = candles.map { it.close }
        val highs = candles.map { it.high }
        val lows = candles.map { it.low }
        val ema4 = ema(closes, config.emaFastPeriod)
        val ema11 = ema(closes, config.emaMidPeriod)
        val ema18 = ema(closes, config.emaSlowPeriod)
        val atr = atr(highs, lows, closes, config.atrPeriod)
        val adx = adx(highs, lows, closes, config.adxPeriod)

        return candles.indices.map { i ->
            val e4 = ema4[i]
            val e11 = ema11[i]
            val e18 = ema18[i]
            val a = atr[i]
            val dx = adx[i]
            val prev11 = valueAt(ema11, i - 1)
            val prevPrev11 = valueAt(ema11, i - 2)
            val slope11 = difference(ema11, i, config.slopeLookback)
            val slope18 = difference(ema18, i, config.slopeLookback)
            val deltaNow = if (e11 != null && prev11 != null) e11 - prev11 else null
            val deltaPrev = if (prev11 != null && prevPrev11 != null) prev11 - prevPrev11 else null
            val range = candles[i].high - candles[i].low
            val bodyRatio = if (range > 0.0) abs(candles[i].close - candles[i].open) / range else 0.0
            val valid = listOf(e4, e11, e18, a, dx, slope11, slope18, deltaNow, deltaPrev).all { it != null }
            val distance = if (e4 != null && e18 != null) e4 - e18 else null
            val strength11 = if (slope11 != null && a != null && a != 0.0) slope11 / a else null
            val strength18 = if (slope18 != null && a != null && a != 0.0) slope18 / a else null
            val candle = candles[i]
            val long = if (!valid) false else {
                val fast = e4!!
                val mid = e11!!
                val slow = e18!!
                val atrValue = a!!
                val slopeMid = slope11!!
                val slopeSlow = slope18!!
                val strengthMid = strength11!!
                val strengthSlow = strength18!!
                val deltaCurrent = deltaNow!!
                val deltaPrevious = deltaPrev!!
                val adxValue = dx!!
                candle.close > fast && candle.close > mid && candle.close > slow &&
                    fast > mid && mid > slow && distance!! > config.distanceAtrMultiplier * atrValue &&
                    slopeSlow > 0 && slopeMid > 0 &&
                    slopeSlow >= config.ema18SlopeAtrMultiplier * atrValue &&
                    slopeMid >= config.ema11SlopeAtrMultiplier * atrValue &&
                    strengthMid > strengthSlow && deltaCurrent > deltaPrevious &&
                    adxValue > config.adxThreshold && range > 0 && bodyRatio >= config.fullBodyMinRatio
            }
            val short = if (!valid) false else {
                val fast = e4!!
                val mid = e11!!
                val slow = e18!!
                val atrValue = a!!
                val slopeMid = slope11!!
                val slopeSlow = slope18!!
                val strengthMid = strength11!!
                val strengthSlow = strength18!!
                val deltaCurrent = deltaNow!!
                val deltaPrevious = deltaPrev!!
                val adxValue = dx!!
                candle.close < fast && candle.close < mid && candle.close < slow &&
                    fast < mid && mid < slow && distance!! < -config.distanceAtrMultiplier * atrValue &&
                    slopeSlow < 0 && slopeMid < 0 &&
                    slopeSlow <= -config.ema18SlopeAtrMultiplier * atrValue &&
                    slopeMid <= -config.ema11SlopeAtrMultiplier * atrValue &&
                    strengthMid < strengthSlow && deltaCurrent < deltaPrevious &&
                    adxValue > config.adxThreshold && range > 0 && bodyRatio >= config.fullBodyMinRatio
            }

            IndicatorCandle(candle, e4, e11, e18, a, dx, slope11, slope18,
                strength11, strength18, deltaNow, deltaPrev, long, short)
        }
    }

    fun evaluate(candles: List<Candle>, position: PositionContext? = null): Decision {
        val latest = indicators(candles).lastOrNull() ?: return Decision()
        val ema11 = latest.ema11 ?: return Decision()
        if (position != null) {
            val breached = when (position.direction) {
                Direction.LONG -> latest.candle.low < ema11
                Direction.SHORT -> latest.candle.high > ema11
            }
            return if (breached) Decision(Action.EXIT, "EMA11_EXIT", latest.candle.timestamp) else Decision()
        }
        return when {
            latest.longSetup -> Decision(Action.ENTER_LONG, timestamp = latest.candle.timestamp)
            latest.shortSetup -> Decision(Action.ENTER_SHORT, timestamp = latest.candle.timestamp)
            else -> Decision(timestamp = latest.candle.timestamp)
        }
    }

    private fun ema(values: List<Double>, period: Int): List<Double?> {
        val out = MutableList<Double?>(values.size) { null }
        if (values.size < period) return out
        var current = values.take(period).average()
        out[period - 1] = current
        val alpha = 2.0 / (period + 1.0)
        for (i in period until values.size) {
            current = alpha * values[i] + (1.0 - alpha) * current
            out[i] = current
        }
        return out
    }

    private fun atr(high: List<Double>, low: List<Double>, close: List<Double>, period: Int): List<Double?> {
        val out = MutableList<Double?>(close.size) { null }
        if (close.size <= period) return out
        val tr = close.indices.map { i ->
            if (i == 0) high[i] - low[i]
            else max(high[i] - low[i], max(abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
        }
        var current = tr.subList(1, period + 1).average()
        out[period] = current
        for (i in period + 1 until close.size) {
            current = ((current * (period - 1)) + tr[i]) / period
            out[i] = current
        }
        return out
    }

    private fun adx(high: List<Double>, low: List<Double>, close: List<Double>, period: Int): List<Double?> {
        val n = close.size
        val out = MutableList<Double?>(n) { null }
        if (n <= period * 2) return out
        val tr = MutableList(n) { 0.0 }
        val plusDm = MutableList(n) { 0.0 }
        val minusDm = MutableList(n) { 0.0 }
        for (i in 1 until n) {
            val up = high[i] - high[i - 1]
            val down = low[i - 1] - low[i]
            plusDm[i] = if (up > down && up > 0) up else 0.0
            minusDm[i] = if (down > up && down > 0) down else 0.0
            tr[i] = max(high[i] - low[i], max(abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
        }
        var smTr = tr.subList(1, period + 1).sum()
        var smPlus = plusDm.subList(1, period + 1).sum()
        var smMinus = minusDm.subList(1, period + 1).sum()
        val dx = MutableList<Double?>(n) { null }
        fun directionalIndex(i: Int): Double {
            if (smTr == 0.0) return 0.0
            val pdi = 100.0 * smPlus / smTr
            val mdi = 100.0 * smMinus / smTr
            return if (pdi + mdi == 0.0) 0.0 else 100.0 * abs(pdi - mdi) / (pdi + mdi)
        }
        dx[period] = directionalIndex(period)
        for (i in period + 1 until n) {
            smTr = smTr - smTr / period + tr[i]
            smPlus = smPlus - smPlus / period + plusDm[i]
            smMinus = smMinus - smMinus / period + minusDm[i]
            dx[i] = directionalIndex(i)
        }
        if (n <= period * 2) return out
        var firstAdx = dx.slice(period until (period * 2)).filterNotNull().average()
        out[period * 2 - 1] = firstAdx
        for (i in period * 2 until n) {
            val currentDx = dx[i] ?: continue
            firstAdx = ((firstAdx * (period - 1)) + currentDx) / period
            out[i] = firstAdx
        }
        return out
    }

    private fun valueAt(values: List<Double?>, i: Int): Double? = if (i in values.indices) values[i] else null
    private fun difference(values: List<Double?>, i: Int, lookback: Int): Double? {
        val now = valueAt(values, i) ?: return null
        val then = valueAt(values, i - lookback) ?: return null
        return now - then
    }
}
