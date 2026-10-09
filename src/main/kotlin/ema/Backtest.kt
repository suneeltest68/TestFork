package ema

import java.nio.file.Files
import java.nio.file.Path
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import kotlin.math.max

data class BacktestTrade(
    val direction: Direction,
    val entryTime: LocalDateTime,
    val entryPrice: Double,
    val exitTime: LocalDateTime,
    val exitPrice: Double,
    val reason: String
) {
    val pnl: Double get() = if (direction == Direction.LONG) exitPrice - entryPrice else entryPrice - exitPrice
}

data class BacktestReport(val trades: List<BacktestTrade>, val startingCapital: Double = 100_000.0) {
    val netPnl = trades.sumOf { it.pnl }
    val wins = trades.count { it.pnl > 0.0 }
    val losses = trades.count { it.pnl <= 0.0 }
    val winRatePercent = if (trades.isEmpty()) 0.0 else 100.0 * wins / trades.size
    val profitFactor: Double = trades.filter { it.pnl > 0 }.sumOf { it.pnl } /
        (trades.filter { it.pnl < 0 }.sumOf { -it.pnl }).let { if (it == 0.0) Double.POSITIVE_INFINITY else it }

    fun printSummary() {
        println("Trades: ${trades.size} | Wins: $wins | Losses: $losses | Win rate: %.2f%%".format(winRatePercent))
        println("Underlying points P&L (before costs): %.2f".format(netPnl))
        println("Profit factor: ${if (profitFactor.isInfinite()) "∞" else "%.2f".format(profitFactor)}")
        println("Note: this is underlying-price signal P&L, not option-premium or brokerage-adjusted returns.")
    }
}

object Backtest {
    fun run(strategy: EmaTrendStrategy, candles: List<Candle>): BacktestReport {
        val trades = mutableListOf<BacktestTrade>()
        var position: Direction? = null
        var entry: Candle? = null
        val enriched = strategy.indicators(candles)
        enriched.forEach { bar ->
            val current = bar.candle
            val e11 = bar.ema11
            if (position != null && e11 != null) {
                val exit = when (position) {
                    Direction.LONG -> current.low < e11
                    Direction.SHORT -> current.high > e11
                    null -> false
                }
                if (exit) {
                    val opened = entry!!
                    trades += BacktestTrade(position!!, opened.timestamp, opened.close, current.timestamp, e11, "EMA11_EXIT")
                    position = null
                    entry = null
                }
            } else {
                when {
                    bar.longSetup -> { position = Direction.LONG; entry = current }
                    bar.shortSetup -> { position = Direction.SHORT; entry = current }
                }
            }
        }
        return BacktestReport(trades)
    }

    fun readCsv(path: Path): List<Candle> {
        require(Files.isRegularFile(path)) { "CSV file not found: $path" }
        val rows = Files.readAllLines(path).filter { it.isNotBlank() }
        require(rows.size > 1) { "CSV must contain a header and at least one candle" }
        val header = rows.first().split(",").map { it.trim().lowercase() }
        fun column(vararg names: String) = names.mapNotNull { header.indexOf(it).takeIf { idx -> idx >= 0 } }.firstOrNull()
            ?: error("CSV missing required column; expected one of ${names.joinToString()}")
        val ti = column("timestamp", "datetime", "date", "time")
        val oi = column("open"); val hi = column("high"); val li = column("low"); val ci = column("close")
        val formatter = DateTimeFormatter.ISO_DATE_TIME
        return rows.drop(1).mapNotNull { line ->
            val cells = line.split(",").map { it.trim() }
            try {
                val rawTime = cells[ti]
                val timestamp = try { LocalDateTime.parse(rawTime, formatter) } catch (_: Exception) {
                    java.time.LocalDate.parse(rawTime).atStartOfDay()
                }
                Candle(timestamp, cells[oi].toDouble(), cells[hi].toDouble(), cells[li].toDouble(), cells[ci].toDouble())
            } catch (e: Exception) {
                throw IllegalArgumentException("Invalid CSV row: $line", e)
            }
        }.sortedBy { it.timestamp }.distinctBy { it.timestamp }
    }

    fun resample(candles: List<Candle>, minutes: Int = 5): List<Candle> {
        require(minutes > 0)
        return candles.sortedBy { it.timestamp }
            .groupBy { it.timestamp.withMinute((it.timestamp.minute / minutes) * minutes).withSecond(0).withNano(0) }
            .toSortedMap()
            .map { (bucket, bars) -> Candle(bucket, bars.first().open, bars.maxOf { it.high }, bars.minOf { it.low }, bars.last().close) }
            .filter { bucket -> bucket.timestamp.minute % minutes == 0 }
    }
}
