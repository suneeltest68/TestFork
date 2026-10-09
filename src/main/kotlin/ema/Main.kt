package ema

import java.nio.file.Path
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.util.concurrent.CountDownLatch

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
    val options = mutableMapOf<String, String>()
    var index = 0
    while (index < args.size) {
        val key = args[index]
        if (key == "--fyers") {
            options[key] = "true"
            index++
        } else {
            require(index + 1 < args.size) { "Missing value for $key" }
            options[key] = args[index + 1]
            index += 2
        }
    }
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
    val fetched = client.history(warmupFrom, today, "1")
    require(fetched.isNotEmpty()) { "FYERS returned no warm-up candles" }
    val now = LocalDateTime.now(ist)
    val currentBucket = now.withMinute((now.minute / 5) * 5).withSecond(0).withNano(0)
    // Drop the currently-forming 5-minute bucket; it will be rebuilt from ticks.
    val warmup = fetched.filter { it.timestamp.isBefore(currentBucket) }
    val paper = PaperTrader()
    val initial = Backtest.resample(warmup, 5)
    val completedWarmup = initial.dropLast(1)
    paper.seedHistory(completedWarmup)
    val currentMinute = java.util.concurrent.atomic.AtomicReference<Candle?>(null)
    val liveCandles = warmup.toMutableList()
    var lastEvaluatedFiveMinute: LocalDateTime = completedWarmup.lastOrNull()?.timestamp ?: LocalDateTime.MIN
    val ticker = client.connectTicker { time, price ->
        val minute = time.withSecond(0).withNano(0)
        synchronized(liveCandles) {
            val previous = currentMinute.get()
            if (previous == null) {
                currentMinute.set(Candle(minute, price, price, price, price))
            } else if (previous.timestamp == minute) {
                currentMinute.set(previous.copy(high = maxOf(previous.high, price), low = minOf(previous.low, price), close = price))
            } else if (minute.isAfter(previous.timestamp)) {
                // The prior minute is now complete; keep it so resampling has the
                // entire session rather than just the most recent tick candle.
                liveCandles.removeAll { it.timestamp == previous.timestamp }
                liveCandles += previous
                val five = Backtest.resample(liveCandles, 5)
                val completed = five.lastOrNull()?.takeIf { it.timestamp.plusMinutes(5).isBefore(minute) || it.timestamp.plusMinutes(5) == minute }
                if (completed != null && completed.timestamp.isAfter(lastEvaluatedFiveMinute)) {
                    lastEvaluatedFiveMinute = completed.timestamp
                    paper.onCompletedFiveMinuteCandle(completed)
                }
                currentMinute.set(Candle(minute, price, price, price, price))
            }
        }
    }
    Runtime.getRuntime().addShutdownHook(Thread { ticker.close() })
    ticker.subscribe()
    println("FYERS paper mode started for ${config.symbol}; no broker orders can be sent. Ctrl+C to stop.")
    CountDownLatch(1).await()
}
