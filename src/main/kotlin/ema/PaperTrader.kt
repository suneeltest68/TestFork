package ema

import java.time.LocalDateTime

data class PaperPosition(val direction: Direction, val entryTime: LocalDateTime, val entryPrice: Double)

class PaperTrader(private val strategy: EmaTrendStrategy = EmaTrendStrategy()) {
    private var position: PaperPosition? = null
    private val history = mutableListOf<Candle>()
    private var lastEvaluated: LocalDateTime? = null

    @Synchronized fun seedHistory(candles: List<Candle>) {
        history.clear()
        history.addAll(candles.sortedBy { it.timestamp }.distinctBy { it.timestamp })
        lastEvaluated = history.lastOrNull()?.timestamp
    }

    @Synchronized fun onCompletedFiveMinuteCandle(candle: Candle) {
        if (lastEvaluated == candle.timestamp) return
        lastEvaluated = candle.timestamp
        history += candle
        val current = position
        val decision = strategy.evaluate(history, current?.let { PositionContext(it.direction) })
        when (decision.action) {
            Action.ENTER_LONG -> {
                position = PaperPosition(Direction.LONG, candle.timestamp, candle.close)
                println("PAPER ENTRY LONG ${candle.timestamp} @ ${candle.close}")
            }
            Action.ENTER_SHORT -> {
                position = PaperPosition(Direction.SHORT, candle.timestamp, candle.close)
                println("PAPER ENTRY SHORT ${candle.timestamp} @ ${candle.close}")
            }
            Action.EXIT -> {
                val open = position ?: return
                val pnl = if (open.direction == Direction.LONG) candle.close - open.entryPrice else open.entryPrice - candle.close
                println("PAPER EXIT ${open.direction} ${candle.timestamp} @ ${candle.close} reason=${decision.exitReason} P&L(points)=%.2f".format(pnl))
                position = null
            }
            Action.HOLD -> Unit
        }
    }

    fun currentPosition(): PaperPosition? = position
}
