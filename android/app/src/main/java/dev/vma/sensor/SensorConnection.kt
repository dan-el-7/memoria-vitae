package dev.vma.sensor

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * One sensor connection to the desktop, over either transport:
 *  - LAN:        OkHttp WebSocket to ws://host/ws/phone
 *  - Relay:      dial-out TCP to the vma_relay, envelope-framed (see
 *                [RelayConnection]); wrapped here so the service sees one API.
 *
 * v2 authentication runs INSIDE the phone→desktop protocol on both
 * transports: hello carries a client nonce; the desktop challenges; the
 * phone answers HMAC-SHA256(cr_secret, nonces) and both sides derive an
 * AES-256-GCM session key. After e2e_start every control message and camera
 * frame is sealed — the relay (and any LAN sniffer) sees only ciphertext.
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
    private val crSecret: String? = null,
) {
    interface Events {
        fun onWelcome(runId: String?, minIntervalMs: Long, heartbeatS: Long, auth: String)
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
    var authLevel: String = "bearer"
        private set

    private var ws: WebSocket? = null
    private var relay: RelayConnection? = null
    private var closedByUser = false
    private var unackedSeq: Long = -1
    private var unackedSinceMs: Long = 0
    private var clientNonce: String = ""
    private var serverNonce: String = ""
    private var seal: VmaCrypto.SealedSession? = null

    // ----------------------------------------------------------- connect

    fun connect() {
        closedByUser = false
        listener.onConnectionState("connecting")
        if (wsUrl.startsWith("relay://")) {
            connectRelay()
        } else {
            connectWs()
        }
    }

    private fun connectWs() {
        val request = Request.Builder().url(wsUrl).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                connected = true
                onTransportOpen { text -> webSocket.send(text) }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                onControlText(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                onServerBinary(bytes.toByteArray())
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

    private fun connectRelay() {
        val conn = RelayConnection(
            wsUrl, token, deviceInfo, crSecret,
            object : RelayConnection.Events {
                override fun onAttached(send: (ByteArray) -> Unit) {
                    connected = true
                    onTransportOpen { text -> send(text.toByteArray(Charsets.UTF_8)) }
                }

                override fun onControl(text: String) = onControlText(text)

                override fun onBinary(data: ByteArray) = onServerBinary(data)

                override fun onState(state: String) {
                    if (state.startsWith("open")) {
                        // attached handled separately
                    } else if (state == "secret-refreshed") {
                        connected = false
                        // Relay rotated the attach secret and dropped us;
                        // reconnect now with the persisted new secret.
                        listener.onConnectionState("relay secret refreshed — reconnecting")
                        onRelaySecretRefreshed()
                    } else {
                        connected = false
                        runActive = false
                        listener.onConnectionState(state)
                    }
                }

                override fun onError(message: String) = listener.onError(message)
            },
            onAttachSecretRefresh = { newSecret -> relayAttachSecretRefresh?.invoke(newSecret) },
        )
        relay = conn
        conn.connect()
    }

    /** Relay handed back a rotated attach secret — reconnect immediately. */
    fun onRelaySecretRefreshed() {
        relay = null
        connected = false
        connect()
    }

    /** Optional hook so the service can persist relay secret rotations. */
    var relayAttachSecretRefresh: ((String) -> Unit)? = null

    // ------------------------------------------------- shared protocol

    /** Transport is up: reset state and send hello (with CR nonce if v2). */
    private fun onTransportOpen(sendText: (String) -> Unit) {
        unackedSeq = -1
        seal = null
        authLevel = "bearer"
        clientNonce = VmaCrypto.nonce()
        val hello = JSONObject()
            .put("type", "hello")
            .put("token", token)
            .put("device", deviceInfo)
        if (!crSecret.isNullOrBlank()) {
            hello.put("cr", JSONObject().put("nonce", clientNonce))
        }
        sendText(hello.toString())
        listener.onConnectionState("open")
    }

    fun onControlText(text: String) {
        try {
            val key = seal
            val plaintext: String = if (key != null) {
                if (!VmaCrypto.isHex(text)) {
                    // Plaintext after e2e_start is either an error frame or a
                    // downgrade attempt — surface it, never parse sealed data as JSON.
                    val probe = JSONObject(text)
                    if (probe.optString("type") == "error") {
                        listener.onError(probe.optString("message", "server error"))
                        return
                    }
                    listener.onError("unsealed control frame after e2e_start")
                    return
                }
                val pt = key.unseal(VmaCrypto.hexToBytes(text), fromPhone = false) ?: run {
                    listener.onError("e2e integrity failure")
                    hardClose()
                    return
                }
                String(pt, Charsets.UTF_8)
            } else text
            handleControl(JSONObject(plaintext))
        } catch (e: Exception) {
            listener.onError("bad control message: ${e.message}")
        }
    }

    private fun handleControl(msg: JSONObject) {
        when (msg.optString("type")) {
            "auth_challenge" -> {
                serverNonce = msg.optString("nonce")
                if (crSecret.isNullOrBlank()) {
                    listener.onError("desktop demanded challenge-response; re-pair to upgrade")
                    hardClose()
                    return
                }
                val response = VmaCrypto.authResponse(crSecret, serverNonce, clientNonce)
                sendControl(JSONObject()
                    .put("type", "auth_response")
                    .put("nonce", clientNonce)
                    .put("response", response)
                    .toString())
            }
            "e2e_start" -> {
                if (!crSecret.isNullOrBlank()) {
                    seal = VmaCrypto.SealedSession(
                        VmaCrypto.deriveSessionKey(crSecret, serverNonce, clientNonce))
                    authLevel = "cr+e2e"
                }
            }
            "welcome" -> {
                runActive = !msg.isNull("run_id") && msg.optString("run_id").isNotBlank()
                val min = msg.optLong("min_interval_ms", 250)
                recommendedIntervalMs = maxOf(min, 250L)
                listener.onWelcome(
                    if (msg.isNull("run_id")) null else msg.optString("run_id"),
                    min,
                    msg.optLong("heartbeat_s", 30),
                    msg.optString("auth", authLevel),
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
            "pong" -> {}
            "error" -> listener.onError(msg.optString("message", "unknown"))
        }
    }

    /** Binary from the server (sealed, or plaintext ack/status frames pre-e2e). */
    fun onServerBinary(data: ByteArray) {
        val key = seal ?: return
        val pt = key.unseal(data, fromPhone = false) ?: run {
            listener.onError("e2e integrity failure (binary)")
            hardClose()
            return
        }
        // Desktop never sends binary control today; if it starts, it is JSON.
        try {
            handleControl(JSONObject(String(pt, Charsets.UTF_8)))
        } catch (_: Exception) {
        }
    }

    // ----------------------------------------------------------- sending

    private fun sendControl(text: String) {
        val s = seal
        if (s != null) {
            sendBytes(s.seal(text.toByteArray(Charsets.UTF_8), phoneToServer = true))
        } else {
            sendTextRaw(text)
        }
    }

    private fun sendTextRaw(text: String): Boolean {
        val w = ws
        if (w != null) return w.send(text)
        val r = relay
        if (r != null) {
            r.sendText(text)
            return true
        }
        return false
    }

    private fun sendBytes(data: ByteArray): Boolean {
        val w = ws
        if (w != null) return w.send(data.toByteString())
        val r = relay
        if (r != null) {
            r.sendBytes(data)
            return true
        }
        return false
    }

    /**
     * Send one frame. Returns false if the transport is down, the desktop has
     * no active run, or a previous frame is still unacked (caller skips).
     */
    fun sendFrame(frame: PendingFrame): Boolean {
        if (!connected || !runActive || unackedSeq != -1L) return false
        val payload = VmaCrypto.buildFramePayload(
            frame.seq, frame.tsDeviceUtc, frame.width, frame.height, frame.jpeg, frame.gps)
        val s = seal
        val ok = if (s != null) {
            val sealed = s.seal(payload, phoneToServer = true)
            sendBytes(sealed)
        } else {
            sendBytes(payload)
        }
        if (ok) {
            unackedSeq = frame.seq
            unackedSinceMs = System.currentTimeMillis()
        }
        return ok
    }

    fun sendHeartbeat(gps: Map<String, Any?>?) {
        val msg = JSONObject().put("type", "heartbeat").put("ts", isoUtcNow())
        if (gps != null) msg.put("gps", JSONObject(gps))
        sendControl(msg.toString())
    }

    fun sendCommand(command: String, args: JSONObject) {
        sendControl(JSONObject()
            .put("type", "command")
            .put("command", command)
            .put("args", args)
            .toString())
    }

    private fun hardClose() {
        try {
            ws?.close(4003, "e2e integrity failure")
            relay?.close()
        } catch (_: Exception) {
        }
        ws = null
        relay = null
        connected = false
    }

    fun close() {
        closedByUser = true
        ws?.close(1000, "user stop")
        relay?.close()
        ws = null
        relay = null
        connected = false
    }

    // ------------------------------------------------------- stop-and-wait

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
