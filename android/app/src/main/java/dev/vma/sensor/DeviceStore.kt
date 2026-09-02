package dev.vma.sensor

import android.content.Context

/** Persisted pairing + settings. Token identifies this physical device to the desktop. */
class DeviceStore(context: Context) {
    private val prefs = context.getSharedPreferences("vma_device", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString("server_url", "") ?: ""
        set(value) = prefs.edit().putString("server_url", value.trim()).apply()

    var deviceToken: String?
        get() = prefs.getString("device_token", null)
        set(value) = prefs.edit().putString("device_token", value).apply()

    var deviceId: String?
        get() = prefs.getString("device_id", null)
        set(value) = prefs.edit().putString("device_id", value).apply()

    /**
     * Opt-in continuous audio: while sensing, record 30s mic segments and send
     * them to the desktop for transcription into the observation memory.
     * OFF by default (privacy); requires RECORD_AUDIO at runtime and adds the
     * microphone foreground-service type while the sensor service is running.
     */
    var continuousAudio: Boolean
        get() = prefs.getBoolean("continuous_audio", false)
        set(value) = prefs.edit().putBoolean("continuous_audio", value).apply()

    val isPaired: Boolean get() = !deviceToken.isNullOrBlank()

    fun unpair() {
        prefs.edit().remove("device_token").remove("device_id").apply()
    }

    companion object {
        fun normalizeUrl(raw: String): String {
            var url = raw.trim().trimEnd('/')
            if (url.startsWith("http://")) url = url.removePrefix("http://")
            if (url.startsWith("https://")) url = url.removePrefix("https://")
            return url
        }
    }
}
