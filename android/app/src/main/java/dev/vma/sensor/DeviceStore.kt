package dev.vma.sensor

import android.content.Context

/**
 * Persisted pairing + settings. The device token identifies this physical
 * device to the desktop; the CR secret (v2) proves possession WITHOUT ever
 * crossing the wire again after pairing. Both live in private app storage.
 */
class DeviceStore(context: Context) {
    private val prefs = context.getSharedPreferences("vma_device", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString("server_url", "") ?: ""
        set(value) { prefs.edit().putString("server_url", value.trim()).apply() }

    var deviceToken: String?
        get() = prefs.getString("device_token", null)
        set(value) { prefs.edit().putString("device_token", value).apply() }

    var deviceId: String?
        get() = prefs.getString("device_id", null)
        set(value) { prefs.edit().putString("device_id", value).apply() }

    /** v2 challenge-response secret (paired post-v2 desktops only). */
    var crSecret: String?
        get() = prefs.getString("cr_secret", null)
        set(value) { prefs.edit().putString("cr_secret", value).apply() }

    /**
     * Opt-in continuous audio: while sensing, record 30s mic segments and send
     * them to the desktop for transcription into the observation memory.
     * OFF by default (privacy); requires RECORD_AUDIO at runtime and adds the
     * microphone foreground-service type while the sensor service is running.
     */
    var continuousAudio: Boolean
        get() = prefs.getBoolean("continuous_audio", false)
        set(value) { prefs.edit().putBoolean("continuous_audio", value).apply() }

    /**
     * Keep Awake: while the app is open, hold the screen on so streaming +
     * preview continue uninterrupted. Without it the screen may time out
     * (the foreground service keeps sensing either way).
     */
    var keepAwake: Boolean
        get() = prefs.getBoolean("keep_awake", false)
        set(value) { prefs.edit().putBoolean("keep_awake", value).apply() }

    /**
     * Power saving for Keep Awake: black the screen (brightness floor + full
     * black overlay, tap to peek) instead of keeping the preview visible.
     * Default ON — an OLED/AMOLED panel at black uses a fraction of the power.
     */
    var keepAwakePowerSave: Boolean
        get() = prefs.getBoolean("keep_awake_power_save", true)
        set(value) { prefs.edit().putBoolean("keep_awake_power_save", value).apply() }

    /** Connection mode: "lan" (default) or "online" (relay). */
    var mode: String
        get() = prefs.getString("mode", "lan") ?: "lan"
        set(value) { prefs.edit().putString("mode", value).apply() }

    // ------------------------------------------------- relay (online mode)

    var relayHost: String
        get() = prefs.getString("relay_host", "") ?: ""
        set(value) { prefs.edit().putString("relay_host", value.trim()).apply() }

    var relayPort: Int
        get() = prefs.getInt("relay_port", 8765)
        set(value) { prefs.edit().putInt("relay_port", value).apply() }

    var relayChannel: String
        get() = prefs.getString("relay_channel", "") ?: ""
        set(value) { prefs.edit().putString("relay_channel", value.trim()).apply() }

    /** Channel attach secret; the relay may hand back a refreshed one. */
    var relayAttachSecret: String
        get() = prefs.getString("relay_attach_secret", "") ?: ""
        set(value) { prefs.edit().putString("relay_attach_secret", value.trim()).apply() }

    var relayUseTls: Boolean
        get() = prefs.getBoolean("relay_use_tls", false)
        set(value) { prefs.edit().putBoolean("relay_use_tls", value).apply() }

    val isPaired: Boolean get() = !deviceToken.isNullOrBlank()

    val hasRelayConfig: Boolean
        get() = relayHost.isNotBlank() && relayChannel.isNotBlank() && relayAttachSecret.isNotBlank()

    fun unpair() {
        prefs.edit()
            .remove("device_token").remove("device_id").remove("cr_secret")
            .apply()
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
