package dev.vma.sensor

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean
import javax.net.ssl.SSLSocketFactory

/**
 * Phone-side client for the standalone vma_relay (online mode).
 *
 * Wire protocol (relay/vma_relay/protocol.py):
 *  - handshake: one newline-delimited JSON line
 *      phone -> {"role":"phone","channel_id":..,"attach_secret":..,
 *                "wait_for_desktop":true|false}
 *      relay -> {"type":"attached"} | {"type":"attach_secret_refresh",...}
 *               | {"type":"error","message":..}
 *  - then length-prefixed envelopes: [u32 BE length][1B kind][payload]
 *      'J' JSON control (the VMA sensor protocol, sealed after e2e_start)
 *      'B' binary (sealed camera frames)
 *      'E' relay events (ping/pong, attach/detach)
 *      'C' close
 *
 * Resilience: wait_for_desktop parks the connection up to 300s while the
 * desktop is offline; the relay keepalives the TCP stream; a closed/failed
 * socket bubbles to [SensorConnection]'s reconnect loop, which retries with
 * capped backoff — the offline ring buffer keeps capturing meanwhile, and
 * frames resume from the last acked seq+1 (desktop dedups by seq anyway).
 */
class RelayConnection(
    url: String,
    private val token: String,
    private val deviceInfo: JSONObject,
    private val crSecret: String?,
    private val events: Events,
    /** Persist a relay-rotated attach secret (device prefs). */
    private val onAttachSecretRefresh: (String) -> Unit = {},
) {
    interface Events {
        /** Transport attached; use the returned sender for control JSON. */
        fun onAttached(send: (ByteArray) -> Unit)
        fun onControl(text: String)
        fun onBinary(data: ByteArray)
        fun onState(state: String) // open|closed|failed: reason
        fun onError(message: String)
    }

    private val host: String
    private val port: Int
    private val tls: Boolean
    private var channel: String = ""
    private var attachSecret: String = ""

    init {
        // relay://host:port/channel/attachsecret  OR  relay://host:port
        val rest = url.removePrefix("relay://")
        val hostPort = rest.substringBefore('/')
        val pathParts = rest.substringAfter('/', "").split('/')
        host = hostPort.substringBefore(':')
        port = hostPort.substringAfter(':', "").toIntOrNull() ?: 8765
        tls = false
        if (pathParts.isNotEmpty() && pathParts[0].isNotBlank()) channel = pathParts[0]
        if (pathParts.size > 1) attachSecret = pathParts[1]
    }

    private var socket: Socket? = null
    private var out: DataOutputStream? = null
    private var `in`: DataInputStream? = null
    private val running = AtomicBoolean(false)
    private var sendLock = Object()

    /** Relay handed back a fresh attach secret; caller should reconnect. */
    class RelaySecretRefreshed(val secret: String) : java.io.IOException("attach secret refreshed")

    fun connect() {
        if (!running.compareAndSet(false, true)) return
        Thread {
            try {
                events.onState("connecting")
                val sock: Socket = if (tls) SSLSocketFactory.getDefault().createSocket()
                else Socket()
                sock.connect(InetSocketAddress(host, port), 15_000)
                sock.tcpNoDelay = true
                sock.keepAlive = true
                socket = sock
                out = DataOutputStream(BufferedOutputStream(sock.getOutputStream()))
                `in` = DataInputStream(BufferedInputStream(sock.getInputStream()))

                val handshake = JSONObject()
                    .put("role", "phone")
                    .put("channel_id", channel)
                    .put("attach_secret", attachSecret)
                    .put("wait_for_desktop", true)
                writeLine(handshake.toString())

                // Read handshake response lines until attached.
                while (true) {
                    val line = readLine() ?: throw java.io.IOException("relay closed")
                    val resp = JSONObject(line)
                    when (resp.optString("type")) {
                        "attach_secret_refresh" -> {
                            // Relay rotated the secret: persist the new one and
                            // RECONNECT (the relay drops this socket on
                            // purpose; continuing would desync the wire).
                            attachSecret = resp.optString("attach_secret")
                            onAttachSecretRefresh(attachSecret)
                            throw RelaySecretRefreshed(attachSecret)
                        }
                        "attached" -> {
                            events.onState("open")
                            val sender = { data: ByteArray -> sendBytes(data) }
                            events.onAttached(sender)
                            readLoop()
                            return@Thread
                        }
                        "error" -> throw java.io.IOException(resp.optString("message", "relay error"))
                    }
                }
            } catch (e: RelaySecretRefreshed) {
                events.onState("secret-refreshed")  // caller reconnects with the new secret
            } catch (e: Exception) {
                events.onState("failed: ${e.message ?: "relay connection failed"}")
            } finally {
                running.set(false)
                closeSocket()
            }
        }.apply { name = "vma-relay-conn"; isDaemon = true }.start()
    }

    private fun readLoop() {
        val input = `in` ?: return
        while (running.get()) {
            try {
                val len = input.readInt()          // u32 BE
                if (len < 1 || len > 24 * 1024 * 1024) throw java.io.IOException("bad envelope")
                val kind = input.readByte().toInt().toChar()
                val payload = ByteArray(len - 1)
                input.readFully(payload)
                when (kind) {
                    'J' -> events.onControl(String(payload, Charsets.UTF_8))
                    'B' -> events.onBinary(payload)
                    'E' -> handleRelayEvent(payload)
                    'C' -> { events.onState("closed (relay)"); return }
                    else -> {}
                }
            } catch (e: Exception) {
                events.onState("failed: ${e.message ?: "relay stream error"}")
                return
            }
        }
    }

    private fun handleRelayEvent(payload: ByteArray) {
        try {
            val ev = JSONObject(String(payload, Charsets.UTF_8))
            when (ev.optString("event")) {
                "ping" -> sendEvent("pong")
                "phone_detached", "desktop_detached" -> events.onState("closed (peer detached)")
            }
        } catch (_: Exception) {
        }
    }

    private fun sendEvent(event: String) {
        sendEnvelope('E', JSONObject().put("event", event).toString().toByteArray(Charsets.UTF_8))
    }

    fun sendText(text: String) = sendEnvelope('J', text.toByteArray(Charsets.UTF_8))

    fun sendBytes(data: ByteArray) = sendEnvelope('B', data)

    private fun sendEnvelope(kind: Char, payload: ByteArray) {
        val o = out ?: return
        synchronized(sendLock) {
            try {
                o.writeInt(1 + payload.size)
                o.write(kind.code)
                o.write(payload)
                o.flush()
            } catch (e: Exception) {
                events.onState("failed: ${e.message ?: "send failed"}")
            }
        }
    }

    private fun writeLine(line: String) {
        val o = out ?: return
        synchronized(sendLock) {
            o.write((line + "\n").toByteArray(Charsets.UTF_8))
            o.flush()
        }
    }

    private fun readLine(): String? {
        val input = `in` ?: return null
        val sb = StringBuilder()
        while (true) {
            val b = input.read()
            if (b < 0) return null
            if (b == '\n'.code) return sb.toString()
            if (sb.length > 16 * 1024) throw java.io.IOException("handshake line too long")
            sb.append(b.toChar())
        }
    }

    fun close() {
        running.set(false)
        closeSocket()
    }

    private fun closeSocket() {
        try {
            socket?.close()
        } catch (_: Exception) {
        }
        socket = null
        out = null
        `in` = null
    }
}
