package ema

import java.nio.file.Path
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicReference

private val ist = ZoneId.of("Asia/Kolkata")

fun main(args: Array<String>) {
    when (args.firstOrNull()?.lowercase()) {
        "backtest" -> runBacktest(args.drop(1))
        "paper" -> runPaper()
        else -> {
            println("EMA Trend Kotlin / FYERS")
            println("Commands:")
            println("  backtest --csv path/to/candles.csv")
            println("  backtest --fyers --from 2026-01-01 --to 2026-01-31")
            println("  paper   (FYERS WebSocket prices + REST candle warm-up; no orders)")
        }
    }
}

private fun runBacktest(args: List<String>) {
    val options = args.chunked(2).filter { it.size == 2 }.associate { it[0] to it[1] }
    val candles = when {
        options.containsKey("--csv") -> Backtest.readCsv(Path.of(options.getValue("--csv")))
        options.containsKey("--fyers") -> {
            val cfg = FyersConfig.fromEnvironment()
            val from = LocalDate.parse(options["--from"] ?: error("Missing --from YYYY-MM-DD"))
            val to = LocalDate.parse(options["--to"] ?: error("Missing --to YYYY-MM-DD"))
            FyersClient(cfg).history(from, to, "1")
        }
        else -> error("Choose --csv PATH or --fyers --from YYYY-MM-DD --to YYYY-MM-DD")
    }
    require(candles.isNotEmpty()) { "No candles returned for requested range" }
    val fiveMinute = Backtest.resample(candles, 5)
    println("Loaded ${candles.size} source candles; ${fiveMinute.size} 5-minute candles.")
    Backtest.run(EmaTrendStrategy(), fiveMinute).printSummary()
}

private fun runPaper() {
    val config = FyersConfig.fromEnvironment()
    val client = FyersClient(config)
    val today = LocalDate.now(ist)
    val warmupFrom = today.minusDays(20)
    val warmup = client.history(warmupFrom, today, "1")
    require(warmup.isNotEmpty()) { "FYERS returned no warm-up candles" }
    val paper = PaperTrader()
    val initial = Backtest.resample(warmup, 5)
    initial.dropLast(1).forEach(paper::onCompletedFiveMinuteCandle)
    val currentMinute = AtomicReference<Candle?>(null)
    val lastClosedMinute = AtomicReference<LocalDateTime?>(null)
    val ticker = client.connectTicker { time, price ->
        val minute = time.withSecond(0).withNano(0)
        synchronized(currentMinute) {
            val previous = currentMinute.get()
            if (previous == null) {
                currentMinute.set(Candle(minute, price, price, price, price))
            } else if (previous.timestamp == minute) {
                currentMinute.set(previous.copy(high = maxOf(previous.high, price), low = minOf(previous.low, price), close = price))
            } else {
                if (minute.isAfter(previous.timestamp)) {
                    if (lastClosedMinute.get() != previous.timestamp) {
                        lastClosedMinute.set(previous.timestamp)
                        val source = (warmup + listOf(previous)).distinctBy { it.timestamp }.sortedBy { it.timestamp }
                        val five = Backtest.resample(source, 5)
                        val completed = five.dropLast(1).lastOrNull()
                        if (completed != null && completed.timestamp.isAfter(initial.lastOrNull()?.timestamp ?: LocalDateTime.MIN)) {
                            paper.onCompletedFiveMinuteCandle(completed)
                        }
                    }
                    currentMinute.set(Candle(minute, price, price, price, price))
                }
            }
        }
    }
    Runtime.getRuntime().addShutdownHook(Thread { ticker.close() })
    ticker.subscribe()
    println("FYERS paper mode started for ${config.symbol}; no broker orders can be sent. Ctrl+C to stop.")
    CountDownLatch(1).await()
}
