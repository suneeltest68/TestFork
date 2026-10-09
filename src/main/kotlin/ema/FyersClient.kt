package ema

import com.google.gson.JsonParser
import java.net.URI
import java.net.URLEncoder
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.net.http.WebSocket
import java.nio.charset.StandardCharsets
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.concurrent.CompletionStage
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private val IST: ZoneId = ZoneId.of("Asia/Kolkata")

data class FyersConfig(
    val appId: String,
    val accessToken: String,
    val symbol: String = "NSE:NIFTY50-INDEX"
) {
    companion object {
        fun fromEnvironment(): FyersConfig {
            loadDotEnv()
            val appId = System.getenv("FYERS_APP_ID")?.trim().orEmpty()
            val token = System.getenv("FYERS_ACCESS_TOKEN")?.trim().orEmpty()
            val symbol = System.getenv("FYERS_SYMBOL")?.trim().takeUnless { it.isNullOrBlank() } ?: "NSE:NIFTY50-INDEX"
            require(appId.isNotBlank()) { "Set FYERS_APP_ID in .env or environment" }
            require(token.isNotBlank()) { "Set FYERS_ACCESS_TOKEN in .env or environment" }
            return FyersConfig(appId, token, symbol)
        }
    }
}

private fun loadDotEnv() {
    val file = java.nio.file.Path.of(".env")
    if (!java.nio.file.Files.exists(file)) return
    java.nio.file.Files.readAllLines(file).forEach { line ->
        val trimmed = line.trim()
        if (trimmed.isNotEmpty() && !trimmed.startsWith("#") && trimmed.contains("=")) {
            val key = trimmed.substringBefore("=").trim()
            val value = trimmed.substringAfter("=").trim().trim('"', '\'')
            if (System.getenv(key).isNullOrBlank() && System.getProperty(key).isNullOrBlank()) {
                System.setProperty(key, value)
            }
        }
    }
}

// Read .env values via system properties without mutating process environment.
private fun env(key: String): String? = System.getenv(key)?.takeIf { it.isNotBlank() }
    ?: System.getProperty(key)?.takeIf { it.isNotBlank() }

class FyersClient(private val config: FyersConfig) {
    private val http = HttpClient.newBuilder().connectTimeout(java.time.Duration.ofSeconds(15)).build()
    private fun auth() = "${config.appId}:${config.accessToken}"

    fun history(from: LocalDate, to: LocalDate, resolution: String = "1"): List<Candle> {
        require(!to.isBefore(from)) { "to date must be on or after from date" }
        val params = listOf(
            "symbol" to config.symbol,
            "resolution" to resolution,
            "date_format" to "1",
            "range_from" to from.format(DateTimeFormatter.ISO_LOCAL_DATE),
            "range_to" to to.format(DateTimeFormatter.ISO_LOCAL_DATE),
            "cont_flag" to "1"
        ).joinToString("&") { (k, v) -> "${k}=${URLEncoder.encode(v, StandardCharsets.UTF_8)}" }
        val request = HttpRequest.newBuilder(URI("https://api-t1.fyers.in/data-rest/v3/history/?$params"))
            .header("Authorization", auth())
            .header("Accept", "application/json")
            .timeout(java.time.Duration.ofSeconds(30)).GET().build()
        val response = http.send(request, HttpResponse.BodyHandlers.ofString())
        check(response.statusCode() in 200..299) { "FYERS history HTTP ${response.statusCode()}: ${response.body().take(300)}" }
        val root = JsonParser.parseString(response.body()).asJsonObject
        check(root.get("s")?.asString == "ok") { "FYERS history rejected: ${response.body().take(300)}" }
        val rows = root.getAsJsonArray("candles") ?: return emptyList()
        return rows.mapNotNull { row ->
            if (!row.isJsonArray || row.asJsonArray.size() < 5) return@mapNotNull null
            val a = row.asJsonArray
            val timestamp = LocalDateTime.ofInstant(Instant.ofEpochSecond(a[0].asLong), IST)
            Candle(timestamp, a[1].asDouble, a[2].asDouble, a[3].asDouble, a[4].asDouble)
        }.sortedBy { it.timestamp }.distinctBy { it.timestamp }
    }

    fun connectTicker(onTick: (LocalDateTime, Double) -> Unit): FyersTicker {
        val ticker = FyersTicker(config, onTick)
        ticker.connect()
        return ticker
    }
}

class FyersTicker(
    private val config: FyersConfig,
    private val onTick: (LocalDateTime, Double) -> Unit
) : WebSocket.Listener, AutoCloseable {
    private val client = HttpClient.newHttpClient()
    private val fragments = StringBuilder()
    private val stopped = AtomicBoolean(false)
    private val connected = CountDownLatch(1)
    @Volatile private var socket: WebSocket? = null

    fun connect() {
        socket = client.newWebSocketBuilder().connectTimeout(java.time.Duration.ofSeconds(20))
            .buildAsync(URI("wss://api.fyers.in/socket/v2/data/"), this).join()
        check(connected.await(20, TimeUnit.SECONDS)) { "FYERS WebSocket did not authenticate/connect in time" }
    }

    override fun onOpen(webSocket: WebSocket) {
        socket = webSocket
        webSocket.request(1)
        webSocket.sendText("${config.appId}:${config.accessToken}", true)
    }

    override fun onText(webSocket: WebSocket, data: CharSequence, last: Boolean): CompletionStage<*>? {
        fragments.append(data)
        if (last) {
            val message = fragments.toString()
            fragments.setLength(0)
            try {
                val json = JsonParser.parseString(message)
                if (json.isJsonObject) {
                    val obj = json.asJsonObject
                    val status = obj.get("s")?.takeIf { !it.isJsonNull }?.asString
                    if (status == "ok" && (obj.has("type") || obj.has("code"))) {
                        connected.countDown()
                    }
                    val symbol = obj.get("symbol")?.takeIf { !it.isJsonNull }?.asString
                    val price = sequenceOf("ltp", "lp", "last_traded_price")
                        .mapNotNull { key -> obj.get(key)?.takeIf { !it.isJsonNull }?.asDouble }.firstOrNull()
                    val epoch = sequenceOf("last_traded_time", "timestamp", "exch_feed_time")
                        .mapNotNull { key -> obj.get(key)?.takeIf { !it.isJsonNull }?.asLong }.firstOrNull()
                    if (symbol == config.symbol && price != null && price.isFinite() && price > 0.0) {
                        val time = if (epoch != null) LocalDateTime.ofInstant(Instant.ofEpochSecond(epoch), IST) else LocalDateTime.now(IST)
                        onTick(time, price)
                    }
                }
            } catch (_: Exception) {
                // FYERS also sends text heartbeat/status frames that are not JSON market ticks.
            }
        }
        webSocket.request(1)
        return null
    }

    override fun onPing(webSocket: WebSocket, message: java.nio.ByteBuffer): CompletionStage<*>? {
        webSocket.request(1)
        return webSocket.sendPong(message)
    }

    override fun onError(webSocket: WebSocket, error: Throwable) {
        connected.countDown()
        System.err.println("FYERS WebSocket error: ${error.message}")
    }

    override fun onClose(webSocket: WebSocket, statusCode: Int, reason: String): CompletionStage<*>? {
        connected.countDown()
        if (!stopped.get()) System.err.println("FYERS WebSocket closed ($statusCode): $reason")
        return null
    }

    fun subscribe() {
        val ws = socket ?: error("WebSocket is not connected")
        ws.sendText("""{"symbol":["${config.symbol}"],"type":"symbolUpdate"}""", true).join()
    }

    override fun close() {
        stopped.set(true)
        socket?.sendClose(WebSocket.NORMAL_CLOSURE, "paper runner stopping")?.join()
    }
}
