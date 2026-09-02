package dev.vma.sensor

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.TimeUnit

/**
 * WebSocket connection to the desktop using the VMA sensor protocol:
 *  - text: JSON control messages (hello/welcome/ack/heartbeat/status/error)
 *  - binary: [u32-LE header length][JSON header][JPEG bytes]
 *
 * Reconnects with capped exponential backoff + jitter. Maintains at most ONE
 * unacked frame in flight (stop-and-wait) and adapts the capture interval to
 * the server-recommended `rec_interval_ms` from acks.
 */
class SensorConnection(
    private val client: OkHttpClient,
    private val wsUrl: String,
    private val token: String,
    private val deviceInfo: JSONObject,
    private val listener: Events,
    initialAckedSeq: Long = -1,
) {
    interface Events {
        fun onWelcome(runId: String?, minIntervalMs: Long, heartbeatS: Long)
        fun onAck(seq: Long, verdict: String, recIntervalMs: Long)
        fun onRunStateChanged(active: Boolean)
        fun onConnectionState(state: String) // connecting|open|closed|failed
        fun onError(message: String)
    }

    var lastAckedSeq: Long = initialAckedSeq
        private set
    var connected: Boolean = false
        private set
    var runActive: Boolean = false
        private set
    var recommendedIntervalMs: Long = 1000
        private set

    private var ws: WebSocket? = null
    private var closedByUser = false
    private var unackedSeq: Long = -1
    private var unackedSinceMs: Long = 0

    fun connect() {
        closedByUser = false
        listener.onConnectionState("connecting")
        val request = Request.Builder().url(wsUrl).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                connected = true
                // The previous socket may have died with a frame in flight;
                // stop-and-wait must not carry that stall into the new socket.
                unackedSeq = -1
                val hello = JSONObject()
                    .put("type", "hello")
                    .put("token", token)
                    .put("device", deviceInfo)
                webSocket.send(hello.toString())
                listener.onConnectionState("open")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    handleControl(JSONObject(text))
                } catch (e: Exception) {
                    listener.onError("bad control message: ${e.message}")
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                connected = false
                runActive = false
                listener.onConnectionState("failed: ${t.message ?: "network error"}")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                connected = false
                runActive = false
                listener.onConnectionState("closed ($code)")
            }
        })
    }

    private fun handleControl(msg: JSONObject) {
        when (msg.optString("type")) {
            "welcome" -> {
                runActive = !msg.isNull("run_id") && msg.optString("run_id").isNotBlank()
                val min = msg.optLong("min_interval_ms", 250)
                recommendedIntervalMs = maxOf(min, 250L)
                listener.onWelcome(
                    if (msg.isNull("run_id")) null else msg.optString("run_id"),
                    min,
                    msg.optLong("heartbeat_s", 30),
                )
            }
            "ack" -> {
                val seq = msg.optLong("seq", -1)
                if (seq > lastAckedSeq) lastAckedSeq = seq
                if (seq == unackedSeq) unackedSeq = -1
                val rec = msg.optLong("rec_interval_ms", recommendedIntervalMs)
                if (rec > 0) recommendedIntervalMs = rec
                listener.onAck(seq, msg.optString("verdict", "?"), rec)
            }
            "status" -> {
                val wasActive = runActive
                runActive = msg.optString("run_state") in setOf("running", "degraded", "paused")
                if (runActive != wasActive) listener.onRunStateChanged(runActive)
            }
            "error" -> listener.onError(msg.optString("message", "unknown"))
        }
    }

    /** True while a sent frame has not been acked (stop-and-wait gate). */
    fun hasUnacked(): Boolean = unackedSeq != -1L

    /** How long the current frame has been awaiting its ack (ms), 0 if none. */
    fun unackedAgeMs(): Long =
        if (unackedSeq == -1L) 0 else System.currentTimeMillis() - unackedSinceMs

    /**
     * Watchdog escape: force-clear a stalled stop-and-wait. The frame may be
     * lost or re-acked later; the desktop dedups replays by seq either way.
     */
    fun clearUnacked() {
        unackedSeq = -1
    }

    /**
     * Send one frame. Returns false if the socket is down, the desktop has no
     * active run, or a previous frame is still unacked (caller skips capture).
     */
    fun sendFrame(frame: PendingFrame): Boolean {
        val socket = ws
        if (socket == null || !connected || !runActive || unackedSeq != -1L) return false
        val header = JSONObject()
            .put("seq", frame.seq)
            .put("ts_device", frame.tsDeviceUtc)
            .put("w", frame.width)
            .put("h", frame.height)
        if (frame.gps != null) header.put("gps", JSONObject(frame.gps))
        val headerBytes = header.toString().toByteArray(Charsets.UTF_8)
        val lenPrefix = ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN)
            .putInt(headerBytes.size).array()
        val payload = lenPrefix + headerBytes + frame.jpeg
        val ok = socket.send(payload.toByteString())
        if (ok) {
            unackedSeq = frame.seq
            unackedSinceMs = System.currentTimeMillis()
        }
        return ok
    }

    fun sendHeartbeat(gps: Map<String, Any?>?) {
        val socket = ws ?: return
        val msg = JSONObject().put("type", "heartbeat").put("ts", isoUtcNow())
        if (gps != null) msg.put("gps", JSONObject(gps))
        socket.send(msg.toString())
    }

    fun close() {
        closedByUser = true
        ws?.close(1000, "user stop")
        ws = null
        connected = false
    }

    companion object {
        fun okHttp(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .pingInterval(20, TimeUnit.SECONDS) // dead-connection detection
            .build()

        /** Milliseconds to wait before the next reconnect attempt. */
        fun backoffMs(attempt: Int): Long {
            val base = minOf(1000L shl minOf(attempt, 5), 30_000L) // 1s,2s,4s..30s cap
            val jitter = (Math.random() * 0.3 * base).toLong()
            return base + jitter
        }

        fun isoUtcNow(): String {
            // UTC ISO-8601 with milliseconds, always ending in Z — the desktop
            // string-compares these for time-range queries.
            return java.time.format.DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'")
                .withZone(java.time.ZoneOffset.UTC)
                .format(java.time.Instant.now())
        }
    }
}
