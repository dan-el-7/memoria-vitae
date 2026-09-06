package dev.vma.sensor

import org.json.JSONObject
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

/** In-process status bridge between the service and the activity. */
object StatusBus {
    @Volatile var status: String = "idle"
    @Volatile var stats: String = ""

    fun publish(context: android.content.Context, text: String, isStats: Boolean = false) {
        if (isStats) stats = text else status = text
    }
}

/** HTTP pairing call: POST http://host/api/pair {"code","device_name"} -> token. */
object PairingClient {
    data class Result(
        val ok: Boolean,
        val token: String?,
        val deviceId: String?,
        val crSecret: String?,
        val error: String?,
    )

    fun pair(serverUrl: String, code: String, deviceName: String): Result {
        return try {
            val host = DeviceStore.normalizeUrl(serverUrl)
            val url = URL("http://$host/api/pair")
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            val body = JSONObject().put("code", code.trim().uppercase()).put("device_name", deviceName)
            conn.outputStream.use { out: OutputStream -> out.write(body.toString().toByteArray()) }
            val codeHttp = conn.responseCode
            val text = (if (codeHttp in 200..299) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { it.readText() } ?: ""
            if (codeHttp in 200..299) {
                val json = JSONObject(text)
                Result(true, json.optString("token"), json.optString("device_id"),
                       json.optString("cr_secret"), null)
            } else {
                val msg = try { JSONObject(text).optString("detail") } catch (_: Exception) { text.take(120) }
                Result(false, null, null, null, "HTTP $codeHttp: $msg")
            }
        } catch (e: Exception) {
            Result(false, null, null, null, e.message ?: "network error")
        }
    }

    /** Announce this phone so the desktop human can approve it (mutual flow). */
    fun requestPairing(serverUrl: String, deviceName: String): Result {
        return try {
            val host = DeviceStore.normalizeUrl(serverUrl)
            val url = URL("http://$host/api/pair/request")
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 8000
            conn.readTimeout = 8000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            val body = JSONObject().put("device_name", deviceName)
            conn.outputStream.use { out: OutputStream -> out.write(body.toString().toByteArray()) }
            val codeHttp = conn.responseCode
            val text = (if (codeHttp in 200..299) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { it.readText() } ?: ""
            if (codeHttp in 200..299) {
                Result(true, null, null, null, null)
            } else {
                val msg = try { JSONObject(text).optString("detail") } catch (_: Exception) { text.take(120) }
                Result(false, null, null, null, "HTTP $codeHttp: $msg")
            }
        } catch (e: Exception) {
            Result(false, null, null, null, e.message ?: "network error")
        }
    }
}

/**
 * Mobile -> desktop remote command channel. Reuses the pairing device token
 * (no second auth system); the desktop enforces a strict allowlist
 * (get_status, pause, resume, stop_run, append_note, chat) — no shell, no
 * arbitrary desktop execution.
 */
object CommandClient {
    data class Result(val ok: Boolean, val message: String)

    fun send(serverUrl: String, token: String?, command: String, args: JSONObject): Result {
        if (token.isNullOrBlank()) return Result(false, "not paired")
        return try {
            val host = DeviceStore.normalizeUrl(serverUrl)
            val url = URL("http://$host/api/command")
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 15000
            conn.readTimeout = 120000  // `chat` runs the reasoning LLM
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            val body = JSONObject()
                .put("token", token)
                .put("command", command)
                .put("args", args)
            conn.outputStream.use { out: OutputStream -> out.write(body.toString().toByteArray()) }
            val codeHttp = conn.responseCode
            val text = (if (codeHttp in 200..299) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { it.readText() } ?: ""
            if (codeHttp in 200..299) {
                val json = JSONObject(text)
                val result = json.optJSONObject("result")
                when (command) {
                    "get_status", "get_observations", "get_observation_image", "chat",
                    "list_runs" ->
                        Result(true, result?.toString() ?: "ok")
                    else -> Result(true, "done")
                }
            } else {
                val msg = try { JSONObject(text).optString("detail") } catch (_: Exception) { text.take(120) }
                Result(false, "HTTP $codeHttp: $msg")
            }
        } catch (e: Exception) {
            Result(false, e.message ?: "network error")
        }
    }

    /**
     * Voice note upload: raw body + token header to /api/voice/note. The
     * desktop transcribes locally with whisper and commits the transcript as
     * an observation linked to nearby/similar observations. Audio is never
     * stored server-side. `source` is "push_to_talk" or "continuous".
     */
    fun postVoiceNote(serverUrl: String, token: String?, audio: ByteArray,
                      source: String = "push_to_talk"): Result {
        if (token.isNullOrBlank()) return Result(false, "not paired")
        return try {
            val host = DeviceStore.normalizeUrl(serverUrl)
            val url = URL("http://$host/api/voice/note?source=$source")
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 15000
            conn.readTimeout = 120000  // whisper small on CPU takes a few seconds
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/octet-stream")
            conn.setRequestProperty("X-VMA-Token", token)
            conn.setFixedLengthStreamingMode(audio.size)
            conn.outputStream.use { it.write(audio) }
            val codeHttp = conn.responseCode
            val text = (if (codeHttp in 200..299) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { it.readText() } ?: ""
            if (codeHttp in 200..299) {
                val json = JSONObject(text)
                if (!json.optBoolean("saved", false)) {
                    Result(false, json.optString("reason", "nothing recognized"))
                } else {
                    val links = json.optJSONArray("links")?.length() ?: 0
                    Result(true, "voice note saved · observation ${json.optInt("observation_id")} · linked to $links")
                }
            } else {
                val msg = try { JSONObject(text).optString("detail") } catch (_: Exception) { text.take(120) }
                Result(false, "HTTP $codeHttp: $msg")
            }
        } catch (e: Exception) {
            Result(false, e.message ?: "network error")
        }
    }
}
