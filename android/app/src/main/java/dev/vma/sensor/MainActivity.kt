@file:OptIn(ExperimentalMaterial3Api::class)

package dev.vma.sensor

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.graphics.SurfaceTexture
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Size
import android.view.Surface
import android.view.TextureView
import android.view.WindowManager
import android.widget.Toast
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.Preview
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * VMA mobile client, Material 3 Expressive (Jetpack Compose, material3 1.4.0).
 * Three destinations: Sensor (capture controls + local preview + voice notes),
 * Memory (run browser, observations, images, ask-the-agent), Connect (pairing).
 * All capture/pairing/command logic lives in SensorService/CommandClient —
 * this file is presentation only.
 */
class MainActivity : androidx.activity.ComponentActivity() {

    private lateinit var store: DeviceStore
    private val uiRefresher = Handler(Looper.getMainLooper())

    // ---- observable UI state (no viewmodel-compose in the offline mirror) --
    private val statusRaw = mutableStateOf("")
    private val statsRaw = mutableStateOf("")
    private val paired = mutableStateOf(false)
    private val serverUrlText = mutableStateOf("")
    private val pairingCodeText = mutableStateOf("")
    private val tabIndex = mutableStateOf(0)
    private val busy = mutableStateOf<String?>(null)
    private val message = mutableStateOf("")
    private val runs = mutableStateOf<List<RunEntry>>(emptyList())
    private val selectedRunId = mutableStateOf<String?>(null)
    private val observations = mutableStateOf<List<ObsEntry>>(emptyList())
    private val obsImage = mutableStateOf<android.graphics.Bitmap?>(null)
    private val answerText = mutableStateOf("")
    private val askInput = mutableStateOf("")
    private val previewOn = mutableStateOf(false)
    private val recording = mutableStateOf(false)
    private val continuousAudio = mutableStateOf(false)
    private val keepAwake = mutableStateOf(false)
    private val keepAwakePowerSave = mutableStateOf(true)
    private val screenBlacked = mutableStateOf(false)
    private val menuOpen = mutableStateOf(false)
    private val confirmUnpair = mutableStateOf(false)
    private val modeText = mutableStateOf("lan")           // lan | online
    private val approvalRequested = mutableStateOf(false)
    private val nearbyDesktops = mutableStateOf<List<VmaNsdHelper.Desktop>>(emptyList())
    private var nsdHelper: VmaNsdHelper? = null

    private data class RunEntry(val id: String, val label: String)
    private data class ObsEntry(val id: Int, val time: String, val importance: Int,
                                val kind: String, val summary: String,
                                val hasMedia: Boolean, val links: Int)

    private var mediaRecorder: MediaRecorder? = null
    private var voiceFile: File? = null
    private var pendingDeepLink: Intent? = null
    private var pendingAudioEnable = false

    private val voicePermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            when {
                granted && pendingAudioEnable -> {
                    pendingAudioEnable = false
                    store.continuousAudio = true
                    SensorService.setContinuousAudio(true)
                    message.value = "Continuous audio on — mic segments are transcribed on the desktop"
                }
                granted -> startVoiceNote()
                else -> {
                    pendingAudioEnable = false
                    message.value = "Microphone permission denied — voice features are opt-in"
                }
            }
        }

    private val allPermsLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { }

    // ------------------------------------------------------------- lifecycle

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = DeviceStore(this)
        serverUrlText.value = store.serverUrl
        keepAwake.value = store.keepAwake
        keepAwakePowerSave.value = store.keepAwakePowerSave
        modeText.value = store.mode
        pendingDeepLink = intent
        setContent { VmaTheme { VmaApp() } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDeepLink(intent)
    }

    override fun onDestroy() {
        super.onDestroy()
        nsdHelper?.stop()
        nsdHelper = null
    }

    /** Browse for desktops advertising _vma._tcp on this Wi-Fi. */
    private fun startNsdDiscovery() {
        if (nsdHelper == null) {
            nsdHelper = VmaNsdHelper(this).apply {
                onUpdate = {
                    runOnUiThread {
                        nearbyDesktops.value = found.values.sortedBy { it.desktopName }
                    }
                }
            }
        }
        nsdHelper?.start()
    }

    private fun stopNsdDiscovery() {
        nsdHelper?.stop()
        nearbyDesktops.value = emptyList()
    }

    override fun onResume() {
        super.onResume()
        refreshPoller()
        uiRefresher.postDelayed(object : Runnable {
            override fun run() {
                refreshPoller()
                uiRefresher.postDelayed(this, 1000)
            }
        }, 1000)
        pendingDeepLink?.let { handleDeepLink(it); pendingDeepLink = null }
    }

    override fun onPause() {
        super.onPause()
        uiRefresher.removeCallbacksAndMessages(null)
        if (previewOn.value) SensorService.setLocalPreview(null) // preview lives on-screen only
    }

    private fun refreshPoller() {
        statusRaw.value = StatusBus.status
        statsRaw.value = StatusBus.stats
        paired.value = store.isPaired
        continuousAudio.value = store.continuousAudio
    }

    private fun handleDeepLink(intent: Intent?) {
        val data = intent?.data ?: return
        if (data.scheme != "vma" || data.host != "pair") return
        val host = data.getQueryParameter("host") ?: return
        val code = data.getQueryParameter("code")
        tabIndex.value = 2
        // Online QR: mode=online&relay=..&rport=..&channel=..&attach=..
        if (data.getQueryParameter("mode") == "online") {
            val relayHost = data.getQueryParameter("relay") ?: ""
            val relayPort = data.getQueryParameter("rport")?.toIntOrNull() ?: 8765
            val channel = data.getQueryParameter("channel") ?: ""
            val attach = data.getQueryParameter("attach") ?: ""
            if (relayHost.isNotBlank() && channel.isNotBlank()) {
                store.relayHost = relayHost
                store.relayPort = relayPort
                store.relayChannel = channel
                store.relayAttachSecret = attach
                store.mode = "online"
                modeText.value = "online"
            }
        } else {
            serverUrlText.value = host
            store.mode = "lan"
            modeText.value = "lan"
        }
        if (!code.isNullOrBlank()) {
            pairingCodeText.value = code
            if (!store.isPaired) pair()
        }
    }

    // ----------------------------------------------------------- actions

    private fun pair() {
        val serverUrl = serverUrlText.value.trim()
        val code = pairingCodeText.value.trim()
        if (serverUrl.isEmpty() || code.isEmpty()) {
            message.value = "Enter the desktop URL and pairing code"
            return
        }
        if (!hasPermission(Manifest.permission.CAMERA)) {
            requestAllPermissions()
            return
        }
        store.serverUrl = serverUrl
        busy.value = "pair"
        Thread {
            val result = PairingClient.pair(serverUrl, code, "${Build.MANUFACTURER} ${Build.MODEL}")
            runOnUiThread {
                busy.value = null
                if (result.ok && !result.token.isNullOrBlank()) {
                    store.deviceToken = result.token
                    store.deviceId = result.deviceId
                    if (!result.crSecret.isNullOrBlank()) {
                        store.crSecret = result.crSecret
                    } else {
                        store.crSecret = null  // legacy desktop: bearer auth only
                    }
                    val e2e = if (!result.crSecret.isNullOrBlank()) " · end-to-end encrypted ready" else ""
                    message.value = "Paired ✓$e2e — start sensing from the Sensor tab"
                } else {
                    message.value = "Pairing failed: ${result.error}"
                }
                paired.value = store.isPaired
            }
        }.start()
    }

    /** Mutual-approval flow: ask the desktop human to approve this phone. */
    private fun requestApproval() {
        val serverUrl = serverUrlText.value.trim()
        if (serverUrl.isEmpty()) {
            message.value = "Enter the desktop URL first"
            return
        }
        store.serverUrl = serverUrl
        busy.value = "request"
        Thread {
            val result = PairingClient.requestPairing(serverUrl, "${Build.MANUFACTURER} ${Build.MODEL}")
            runOnUiThread {
                busy.value = null
                if (result.ok) {
                    message.value = "Approval requested — the desktop operator must approve, " +
                        "then you'll get a code to enter here"
                    approvalRequested.value = true
                } else {
                    message.value = "Request failed: ${result.error}"
                }
            }
        }.start()
    }

    private fun commandArgs(): JSONObject {
        val args = JSONObject()
        selectedRunId.value?.let { args.put("run_id", it) }
        return args
    }

    private fun sendCommand(command: String, args: JSONObject) {
        val serverUrl = store.serverUrl
        val token = store.deviceToken
        busy.value = command
        Thread {
            val result = CommandClient.send(serverUrl, token, command, args)
            runOnUiThread {
                busy.value = null
                if (!result.ok) {
                    message.value = "✗ $command: ${result.message}"
                    return@runOnUiThread
                }
                when (command) {
                    "list_runs" -> applyRuns(result.message)
                    "get_observations" -> applyObservations(result.message)
                    "get_observation_image" -> applyImage(result.message)
                    "chat" -> answerText.value = runCatching {
                        JSONObject(result.message).optString("answer")
                    }.getOrDefault(result.message).ifBlank { "(empty answer)" }
                    "get_status" -> message.value = runCatching {
                        val o = JSONObject(result.message)
                        "state=${o.optString("run_state")} · connected=${o.optBoolean("sensor_connected")}"
                    }.getOrDefault(result.message)
                    "mark_moment" -> message.value = runCatching {
                        val o = JSONObject(result.message)
                        "★ Marked ${o.optInt("marked")} observation(s) from the last ${o.optInt("window_seconds")}s"
                    }.getOrDefault(result.message)
                    else -> message.value = result.message.ifBlank { "done" }
                }
            }
        }.start()
    }

    private fun applyRuns(json: String) {
        runCatching {
            val arr = JSONObject(json).optJSONArray("runs") ?: JSONArray()
            val entries = (0 until arr.length()).map { i ->
                val r = arr.getJSONObject(i)
                val label = (if (r.optBoolean("active")) "● " else "") +
                    r.optString("name").ifBlank { r.optString("run_id") } +
                    " · " + r.optString("created_at").take(10) +
                    " · " + r.optDouble("size_mb", 0.0) + " MB"
                RunEntry(r.optString("run_id"), label)
            }
            runs.value = entries
            if (entries.none { it.id == selectedRunId.value }) selectedRunId.value = null
            message.value = "${entries.size} run(s) — pick one, then Recent obs / Last image / Ask"
        }.onFailure { message.value = "✗ runs: ${it.message}" }
    }

    private fun applyObservations(json: String) {
        runCatching {
            val arr = JSONObject(json).optJSONArray("observations") ?: JSONArray()
            observations.value = (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                ObsEntry(
                    id = o.optInt("id"),
                    time = o.optString("local", o.optString("ts"))
                        .let { if (it.length >= 16) it.substring(11, 16) else it },
                    importance = o.optInt("importance"),
                    kind = o.optString("kind"),
                    summary = o.optString("summary"),
                    hasMedia = !o.optString("media").isNullOrBlank(),
                    links = o.optInt("links"),
                )
            }
            obsImage.value = null
            if (observations.value.isEmpty()) message.value = "no observations in this run yet"
        }.onFailure { message.value = "✗ observations: ${it.message}" }
    }

    private fun applyImage(json: String) {
        runCatching {
            val obj = JSONObject(json)
            val bytes = android.util.Base64.decode(obj.optString("image_b64"), android.util.Base64.DEFAULT)
            val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            if (bmp == null) message.value = "✗ image decode failed"
            else {
                obsImage.value = bmp
                message.value = "observation ${obj.optInt("observation_id")} — ${obj.optString("summary")}"
            }
        }.onFailure { message.value = "✗ image: ${it.message}" }
    }

    private fun fetchLastImage() {
        val candidate = observations.value.firstOrNull { it.hasMedia }
        if (candidate == null) {
            message.value = "no retained image in the loaded list — tap Recent obs first"
            return
        }
        sendCommand("get_observation_image", commandArgs().put("observation_id", candidate.id))
    }

    // ------------------------------------------------- voice notes (hold)

    private fun onVoicePressStart() {
        if (!hasPermission(Manifest.permission.RECORD_AUDIO)) {
            voicePermLauncher.launch(Manifest.permission.RECORD_AUDIO)
            return
        }
        startVoiceNote()
    }

    private fun startVoiceNote() {
        if (recording.value) return
        if (!store.isPaired) { message.value = "Pair first"; return }
        if (StatusBus.status == "idle" || StatusBus.status.isBlank()) {
            message.value = "Start sensing first — voice notes attach to the active run"
            return
        }
        val file = File(cacheDir, "voice_note.m4a")
        val rec = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this)
                  else @Suppress("DEPRECATION") MediaRecorder()
        runCatching {
            rec.setAudioSource(MediaRecorder.AudioSource.MIC)
            rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            rec.setAudioSamplingRate(16000)
            rec.setAudioEncodingBitRate(64000)
            rec.setOutputFile(file.absolutePath)
            rec.prepare()
            rec.start()
        }.onFailure {
            rec.release()
            message.value = "✗ recorder: ${it.message}"
            return
        }
        mediaRecorder = rec
        voiceFile = file
        recording.value = true
    }

    private fun onVoicePressEnd() {
        if (!recording.value) return
        val rec = mediaRecorder
        mediaRecorder = null
        recording.value = false
        val file = voiceFile
        voiceFile = null
        if (rec == null || file == null) return
        runCatching { rec.stop() }
        rec.release()
        val bytes = runCatching { file.readBytes() }.getOrDefault(ByteArray(0))
        file.delete() // memory-on-the-wire only; the temp file is gone now
        if (bytes.isEmpty()) { message.value = "recording was empty"; return }
        busy.value = "voice"
        val serverUrl = store.serverUrl
        val token = store.deviceToken
        Thread {
            val result = CommandClient.postVoiceNote(serverUrl, token, bytes)
            runOnUiThread {
                busy.value = null
                message.value = if (result.ok) result.message else "✗ voice note: ${result.message}"
            }
        }.start()
    }

    // ------------------------------------------------------- permissions

    private fun hasPermission(perm: String) =
        ContextCompat.checkSelfPermission(this, perm) == PackageManager.PERMISSION_GRANTED

    private fun requestAllPermissions() {
        val wanted = buildList {
            add(Manifest.permission.CAMERA)
            add(Manifest.permission.ACCESS_FINE_LOCATION)
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
        }
        allPermsLauncher.launch(wanted.toTypedArray())
    }

    // ================================================================ UI

    @Composable
    private fun VmaApp() {
        val chip = stateChip(statusRaw.value)
        // Keep Awake: hold the screen on via window flag; power saving blacks
        // the screen (overlay + brightness floor) while streaming continues.
        DisposableEffect(keepAwake.value, keepAwakePowerSave.value, screenBlacked.value) {
            val win = window
            if (keepAwake.value) win.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else win.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            win.attributes = win.attributes.apply {
                screenBrightness =
                    if (keepAwake.value && keepAwakePowerSave.value && screenBlacked.value) {
                        WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_OFF
                    } else {
                        WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
                    }
            }
            onDispose { }
        }
        Box(Modifier.fillMaxSize()) {
            Scaffold(
            topBar = {
                CenterAlignedTopAppBar(
                    title = { Text("VMA Sensor", fontWeight = FontWeight.SemiBold) },
                    actions = {
                        StateChipSmall(chip)
                        IconButton(onClick = { menuOpen.value = true }) {
                            Text("⋮", style = MaterialTheme.typography.titleLarge)
                        }
                        DropdownMenu(expanded = menuOpen.value, onDismissRequest = { menuOpen.value = false }) {
                            DropdownMenuItem(text = { Text("Refresh runs") }, onClick = {
                                menuOpen.value = false; tabIndex.value = 1
                                sendCommand("list_runs", JSONObject())
                            })
                            DropdownMenuItem(text = { Text("Desktop status") }, onClick = {
                                menuOpen.value = false; sendCommand("get_status", JSONObject())
                            })
                            DropdownMenuItem(text = { Text("Unpair device") }, onClick = {
                                menuOpen.value = false; confirmUnpair.value = true
                            })
                        }
                    },
                )
            },
            bottomBar = {
                NavigationBar {
                    listOf("Sensor", "Memory", "Connect").forEachIndexed { index, label ->
                        NavigationBarItem(
                            selected = tabIndex.value == index,
                            onClick = { tabIndex.value = index },
                            label = { Text(label) },
                            icon = { Text(listOf("◉", "☰", "⌂")[index]) },
                        )
                    }
                }
            },
        ) { padding ->
            Surface(modifier = Modifier.fillMaxSize().padding(padding)) {
                when (tabIndex.value) {
                    0 -> SensorTab()
                    1 -> MemoryTab()
                    else -> ConnectTab()
                }
            }
        }
            // Power-saving black screen: hides everything, tap anywhere to peek.
            if (keepAwake.value && keepAwakePowerSave.value && screenBlacked.value) {
                Box(
                    Modifier
                        .fillMaxSize()
                        .background(Color.Black)
                        .clickable { screenBlacked.value = false },
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        "◉ sensing — screen dimmed · tap to peek",
                        color = Color.White.copy(alpha = 0.25f),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }
        if (confirmUnpair.value) {
            AlertDialog(
                onDismissRequest = { confirmUnpair.value = false },
                title = { Text("Unpair this device?") },
                text = { Text("The desktop keeps the token until you revoke it there. You can re-pair any time.") },
                confirmButton = {
                    TextButton(onClick = {
                        store.unpair(); paired.value = false; confirmUnpair.value = false
                        message.value = "Unpaired"
                    }) { Text("Unpair") }
                },
                dismissButton = {
                    TextButton(onClick = { confirmUnpair.value = false }) { Text("Cancel") }
                },
            )
        }
    }

    @Composable
    private fun SensorTab() {
        val active = StatusBus.status !in setOf("idle", "") && StatusBus.status.isNotBlank()
        Column(
            modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            StateCard(stateChip(statusRaw.value), detail = statusRaw.value.ifBlank { "no sensor yet" })

            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = {
                        store.serverUrl = serverUrlText.value
                        if (!hasPermission(Manifest.permission.CAMERA)) requestAllPermissions()
                        else if (!store.isPaired) message.value = "Pair first (Connect tab)"
                        else { SensorService.start(this@MainActivity); message.value = "Sensor started" }
                    },
                    enabled = store.isPaired && !active,
                    modifier = Modifier.weight(1f).height(48.dp),
                ) { Text("Start sensing") }
                OutlinedButton(
                    onClick = { SensorService.stop(this@MainActivity) },
                    enabled = active,
                    modifier = Modifier.weight(1f).height(48.dp),
                ) { Text("Stop") }
            }

            Card(modifier = Modifier.fillMaxWidth()) {
                Text(
                    statsRaw.value.ifBlank { "waiting for sensor stats…" },
                    modifier = Modifier.padding(14.dp),
                    fontFamily = FontFamily.Monospace,
                    style = MaterialTheme.typography.bodySmall,
                )
            }

            // "That matters, remember it": bumps importance of the last
            // minute of observations so retrieval prioritizes them and media
            // eviction protects their frames.
            Button(
                onClick = {
                    store.serverUrl = serverUrlText.value
                    sendCommand("mark_moment", JSONObject().put("window_seconds", 60))
                },
                enabled = store.isPaired && active,
                modifier = Modifier.fillMaxWidth().height(48.dp),
            ) { Text("★ Mark this moment") }

            // Hold-to-talk voice note → desktop whisper → linked observation.
            val micContainer = if (recording.value) MaterialTheme.colorScheme.errorContainer
            else MaterialTheme.colorScheme.primaryContainer
            val micContent = if (recording.value) MaterialTheme.colorScheme.onErrorContainer
            else MaterialTheme.colorScheme.onPrimaryContainer
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(64.dp)
                    .background(micContainer, RoundedCornerShape(32.dp))
                    .pointerInput(Unit) {
                        detectTapGestures(
                            onPress = {
                                onVoicePressStart()
                                try { awaitRelease() } finally { onVoicePressEnd() }
                            },
                        )
                    },
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    if (recording.value) "🎙 recording… release to send"
                    else "🎙 hold to record a voice note",
                    color = micContent,
                    fontWeight = FontWeight.SemiBold,
                )
            }
            Text(
                "Voice notes are transcribed on the desktop (whisper, CPU), saved as observations, " +
                    "and linked to what the camera saw around them. Audio is never stored.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            // Opt-in continuous audio: while sensing, 30s mic segments are
            // sent and become linked voice observations. OFF by default.
            Row(verticalAlignment = Alignment.CenterVertically) {
                Switch(
                    checked = continuousAudio.value,
                    onCheckedChange = { want ->
                        if (want) {
                            if (hasPermission(Manifest.permission.RECORD_AUDIO)) {
                                store.continuousAudio = true
                                SensorService.setContinuousAudio(true)
                                continuousAudio.value = true
                                message.value = "Continuous audio on — mic segments are transcribed on the desktop"
                            } else {
                                pendingAudioEnable = true
                                voicePermLauncher.launch(Manifest.permission.RECORD_AUDIO)
                            }
                        } else {
                            store.continuousAudio = false
                            SensorService.setContinuousAudio(false)
                            continuousAudio.value = false
                            message.value = "Continuous audio off"
                        }
                    },
                )
                Text(
                    "Continuous audio (while sensing)",
                    modifier = Modifier.padding(start = 10.dp),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
            Text(
                "Off by default. When on, the phone records ~30s mic segments and sends them with " +
                    "your device token for local transcription; silent segments produce nothing, " +
                    "and Android shows its microphone-in-use indicator. Battery use increases.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            // Keep Awake: hold the screen on while the app is open. With the
            // power-saving option, the screen blacks out (tap to peek) so an
            // OLED panel draws almost nothing while streaming continues.
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Button(
                    onClick = {
                        when {
                            !keepAwake.value -> {
                                keepAwake.value = true
                                store.keepAwake = true
                                if (keepAwakePowerSave.value) screenBlacked.value = true
                                message.value = "Keep awake on"
                            }
                            keepAwakePowerSave.value && !screenBlacked.value ->
                                screenBlacked.value = true
                            else -> {
                                keepAwake.value = false
                                store.keepAwake = false
                                screenBlacked.value = false
                                message.value = "Keep awake off"
                            }
                        }
                    },
                ) {
                    Text(
                        when {
                            !keepAwake.value -> "Keep awake"
                            keepAwakePowerSave.value && !screenBlacked.value -> "Dim screen"
                            else -> "Keep awake: ON"
                        }
                    )
                }
            }
            if (keepAwake.value) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Switch(
                        checked = keepAwakePowerSave.value,
                        onCheckedChange = {
                            keepAwakePowerSave.value = it
                            store.keepAwakePowerSave = it
                            if (it) screenBlacked.value = true else screenBlacked.value = false
                        },
                    )
                    Text(
                        "Black screen while awake (power saving)",
                        modifier = Modifier.padding(start = 10.dp),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
                Text(
                    "Screen blacks out and brightness drops to zero; tap anywhere to peek. " +
                        "Streaming continues either way — off keeps the preview visible instead.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            Row(verticalAlignment = Alignment.CenterVertically) {
                Switch(
                    checked = previewOn.value,
                    onCheckedChange = { previewOn.value = it },
                )
                Text(
                    "Local preview (phone screen only)",
                    modifier = Modifier.padding(start = 10.dp),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
            if (previewOn.value) LocalPreview()
        }
    }

    @Composable
    private fun LocalPreview() {
        val textureView = remember { TextureView(this) }
        DisposableEffect(previewOn.value) {
            if (previewOn.value) {
                if (SensorService.instance == null) {
                    Toast.makeText(this@MainActivity, "Start sensing to open the camera", Toast.LENGTH_SHORT)
                        .show()
                }
                // Feed CameraX from the TextureView's OWN SurfaceTexture: the
                // view then renders frames natively, and waiting for the
                // availability listener removes the attach race of a manual
                // SurfaceTexture(0) (which never rendered reliably).
                SensorService.setLocalPreview(Preview.SurfaceProvider { request ->
                    val resolution: Size = request.resolution
                    fun provide(st: SurfaceTexture) {
                        st.setDefaultBufferSize(resolution.width, resolution.height)
                        val surface = Surface(st)
                        request.provideSurface(surface, ContextCompat.getMainExecutor(this@MainActivity)) {
                            surface.release()
                        }
                    }
                    val existing = textureView.surfaceTexture
                    if (existing != null) {
                        provide(existing)
                    } else {
                        textureView.surfaceTextureListener =
                            object : TextureView.SurfaceTextureListener {
                                override fun onSurfaceTextureAvailable(
                                    st: SurfaceTexture, w: Int, h: Int,
                                ) {
                                    textureView.surfaceTextureListener = null
                                    provide(st)
                                }

                                override fun onSurfaceTextureSizeChanged(
                                    st: SurfaceTexture, w: Int, h: Int,
                                ) {}

                                override fun onSurfaceTextureDestroyed(st: SurfaceTexture) = true

                                override fun onSurfaceTextureUpdated(st: SurfaceTexture) {}
                            }
                    }
                })
            } else {
                SensorService.setLocalPreview(null)
            }
            onDispose { SensorService.setLocalPreview(null) }
        }
        AndroidView(
            factory = { textureView },
            modifier = Modifier
                .fillMaxWidth()
                .height(220.dp)
                .clip(RoundedCornerShape(24.dp)),
        )
    }

    @Composable
    private fun MemoryTab() {
        Column(
            modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            RunSelector()
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = { sendCommand("get_observations", commandArgs().put("limit", 20)) },
                    enabled = busy.value == null,
                    modifier = Modifier.weight(1f),
                ) { Text("Recent obs") }
                OutlinedButton(
                    onClick = { fetchLastImage() },
                    enabled = busy.value == null,
                    modifier = Modifier.weight(1f),
                ) { Text("Last image") }
            }
            obsImage.value?.let { bmp ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Image(
                        bitmap = bmp.asImageBitmap(),
                        contentDescription = "observation image",
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
            if (busy.value == "get_observations" || busy.value == "get_observation_image" ||
                busy.value == "list_runs" || busy.value == "voice"
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    CircularProgressIndicator(modifier = Modifier.size(40.dp))
                    Text("working on the desktop…", style = MaterialTheme.typography.bodySmall)
                }
            }
            LazyColumn(modifier = Modifier.fillMaxWidth().height(320.dp)) {
                items(observations.value) { o ->
                    ListItem(
                        headlineContent = { Text(o.summary.ifBlank { "(no summary)" }) },
                        supportingContent = {
                            Text(
                                buildString {
                                    append("#${o.id} · ${o.time} · imp${o.importance}")
                                    if (o.kind == "voice") append(" · 🎙 voice")
                                    if (o.links > 0) append(" · ${o.links} link(s)")
                                    if (o.hasMedia) append(" · 🖼 image")
                                }
                            )
                        },
                    )
                }
            }
            AskSection()
        }
    }

    @Composable
    private fun RunSelector() {
        var expanded by remember { mutableStateOf(false) }
        val entries = runs.value
        val selectedLabel = entries.firstOrNull { it.id == selectedRunId.value }?.label
            ?: "<active run>"
        ExposedDropdownMenuBox(expanded = expanded, onExpandedChange = { expanded = it }) {
            OutlinedTextField(
                value = selectedLabel,
                onValueChange = {},
                readOnly = true,
                label = { Text("Run") },
                trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded) },
                modifier = Modifier.menuAnchor(MenuAnchorType.PrimaryNotEditable).fillMaxWidth(),
            )
            ExposedDropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
                DropdownMenuItem(
                    text = { Text("<active run>") },
                    onClick = { selectedRunId.value = null; expanded = false },
                )
                entries.forEach { entry ->
                    DropdownMenuItem(
                        text = { Text(entry.label) },
                        onClick = { selectedRunId.value = entry.id; expanded = false },
                    )
                }
            }
        }
    }

    @Composable
    private fun AskSection() {
        OutlinedTextField(
            value = askInput.value,
            onValueChange = { askInput.value = it },
            label = { Text("Ask the agent about this run…") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
        )
        Row(
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Button(
                onClick = {
                    val text = askInput.value.trim()
                    if (text.isEmpty()) {
                        message.value = "Type a question first"
                        return@Button
                    }
                    askInput.value = ""
                    answerText.value = ""
                    sendCommand("chat", commandArgs().put("text", text))
                },
                enabled = busy.value == null,
            ) { Text("Send to agent") }
            if (busy.value == "chat") CircularProgressIndicator(modifier = Modifier.size(32.dp))
        }
        if (answerText.value.isNotBlank()) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            ) {
                Text(
                    answerText.value,
                    modifier = Modifier.padding(14.dp),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }
    }

    @Composable
    private fun ConnectTab() {
        Column(
            modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Connect to your desktop", style = MaterialTheme.typography.titleMedium)

            // ------------------------------------------ mode toggle
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(
                    selected = modeText.value == "lan",
                    onClick = { modeText.value = "lan"; store.mode = "lan" },
                    label = { Text("Same Wi-Fi (LAN)") },
                )
                FilterChip(
                    selected = modeText.value == "online",
                    onClick = { modeText.value = "online"; store.mode = "online" },
                    label = { Text("Online (relay)") },
                )
            }

            if (modeText.value == "lan") {
                // ------------------------------------ nearby desktops
                Text("Nearby desktops on this Wi-Fi", style = MaterialTheme.typography.titleSmall)
                Button(onClick = { startNsdDiscovery() }, enabled = busy.value == null) {
                    Text(if (nearbyDesktops.value.isEmpty()) "Scan for desktops" else "Rescan")
                }
                if (nearbyDesktops.value.isEmpty()) {
                    Text(
                        "No desktops found yet. Make sure the desktop app is running and both " +
                            "devices are on the same Wi-Fi, then scan.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                nearbyDesktops.value.forEach { desk ->
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(desk.desktopName, fontWeight = FontWeight.Bold)
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    if (desk.pairingLive) "ready to pair" else "locked",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = if (desk.pairingLive) Color(0xFF3ECF8E) else Color(0xFF9BA3B2),
                                )
                            }
                            Text(
                                "${desk.host}:${desk.port}",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            Spacer(Modifier.height(4.dp))
                            Button(onClick = {
                                serverUrlText.value = "${desk.host}:${desk.port}"
                                store.serverUrl = serverUrlText.value
                            }) { Text("Use this desktop") }
                        }
                    }
                }

                OutlinedTextField(
                    value = serverUrlText.value,
                    onValueChange = { serverUrlText.value = it },
                    label = { Text("…or desktop URL (e.g. 192.168.1.10:8619)") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true,
                )
            } else {
                // ---------------------------------------- online mode
                Text(
                    "Online mode routes through your relay: works from any network, " +
                        "end-to-end encrypted, survives internet drops with the offline buffer.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (store.hasRelayConfig) {
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Text("Relay configured", fontWeight = FontWeight.Bold)
                            Text(
                                "${store.relayHost}:${store.relayPort} · channel ${store.relayChannel.take(10)}…",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                } else {
                    Text(
                        "No relay configured — scan the desktop's ONLINE pairing QR " +
                            "(shown when its relay is connected).",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            OutlinedTextField(
                value = pairingCodeText.value,
                onValueChange = { pairingCodeText.value = it.uppercase() },
                label = { Text("Pairing code (from the dashboard or QR)") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
            )
            Button(
                onClick = { pair() },
                enabled = busy.value == null,
                modifier = Modifier.height(48.dp),
            ) {
                if (busy.value == "pair") CircularProgressIndicator(modifier = Modifier.size(24.dp))
                else Text("Pair")
            }
            if (modeText.value == "lan") {
                OutlinedButton(
                    onClick = { requestApproval() },
                    enabled = busy.value == null,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (busy.value == "request") CircularProgressIndicator(modifier = Modifier.size(20.dp))
                    else Text("Request approval instead (no code needed)")
                }
            }
            Text(
                if (paired.value) {
                    val auth = if (!store.crSecret.isNullOrBlank()) "challenge-response + E2E encryption" else "bearer token (re-pair to upgrade)"
                    "Paired as ${store.deviceId ?: "?"} → " +
                        (if (modeText.value == "online") "relay ${store.relayHost}" else store.serverUrl) +
                        "\nAuth: $auth\n\n" +
                        "Pairing is permanent: credentials never expire until you revoke the device in the " +
                        "desktop dashboard. If the desktop's IP changes, re-scan its QR to update the address."
                } else {
                    "Not paired. Scan the desktop's pairing QR with this phone's camera app — it opens " +
                        "here with everything filled in — or find a nearby desktop above."
                },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }

    @Composable
    private fun StateCard(chip: StateChip, detail: String) {
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = chip.container),
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(
                    chip.label,
                    color = chip.content,
                    fontWeight = FontWeight.Bold,
                    style = MaterialTheme.typography.headlineSmall,
                )
                Text(detail, color = chip.content, style = MaterialTheme.typography.bodySmall)
            }
        }
    }

    @Composable
    private fun StateChipSmall(chip: StateChip) {
        Box(
            modifier = Modifier
                .background(chip.container, RoundedCornerShape(50))
                .padding(horizontal = 10.dp, vertical = 4.dp),
        ) {
            Text(
                chip.label,
                color = chip.content,
                style = MaterialTheme.typography.labelSmall,
                fontWeight = FontWeight.Bold,
            )
        }
    }

    private data class StateChip(val label: String, val container: Color, val content: Color)

    private fun stateChip(raw: String): StateChip = when {
        raw.isBlank() || raw == "idle" -> StateChip("DISCONNECTED", Color(0xFF23262E), Color(0xFF9BA3B2))
        raw.startsWith("error") -> StateChip("ERROR", Color(0xFF3D1F1F), Color(0xFFFF6B6B))
        raw.contains("no active run") || raw.startsWith("connection: closed") ||
            raw.startsWith("connection: failed") || raw.startsWith("reconnecting") ->
            StateChip("DISCONNECTED", Color(0xFF23262E), Color(0xFF9BA3B2))
        raw == "paused" || raw.startsWith("paused") -> StateChip("PAUSED", Color(0xFF3A2E14), Color(0xFFFFB454))
        else -> StateChip("OBSERVING", Color(0xFF12301F), Color(0xFF3ECF8E))
    }
}

// ------------------------------------------------------------ theme (M3E)

private val DarkScheme = darkColorScheme(
    primary = Color(0xFFA8C8FF),
    onPrimary = Color(0xFF0A305F),
    primaryContainer = Color(0xFF26437A),
    onPrimaryContainer = Color(0xFFD8E2FF),
    secondary = Color(0xFFBFC6DC),
    onSecondary = Color(0xFF293041),
    secondaryContainer = Color(0xFF3F4759),
    onSecondaryContainer = Color(0xFFDAE2F9),
    tertiary = Color(0xFF7FD8AC),
    onTertiary = Color(0xFF00391F),
    tertiaryContainer = Color(0xFF00512D),
    onTertiaryContainer = Color(0xFF95F1C4),
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005),
    background = Color(0xFF0F1115),
    onBackground = Color(0xFFE6E9EF),
    surface = Color(0xFF0F1115),
    onSurface = Color(0xFFE6E9EF),
    surfaceVariant = Color(0xFF181B22),
    onSurfaceVariant = Color(0xFF8B93A3),
    outline = Color(0xFF434956),
)

private val LightScheme = lightColorScheme(
    primary = Color(0xFF265CA8),
    onPrimary = Color(0xFFFFFFFF),
    primaryContainer = Color(0xFFD8E2FF),
    onPrimaryContainer = Color(0xFF001A41),
    secondary = Color(0xFF565E71),
    onSecondary = Color(0xFFFFFFFF),
    secondaryContainer = Color(0xFFDAE2F9),
    onSecondaryContainer = Color(0xFF131C2B),
    tertiary = Color(0xFF146C43),
    onTertiary = Color(0xFFFFFFFF),
    tertiaryContainer = Color(0xFF95F1C4),
    onTertiaryContainer = Color(0xFF00210F),
    error = Color(0xFFBA1A1A),
    onError = Color(0xFFFFFFFF),
    background = Color(0xFFF9F9FF),
    onBackground = Color(0xFF1A1C22),
    surface = Color(0xFFF9F9FF),
    onSurface = Color(0xFF1A1C22),
    surfaceVariant = Color(0xFFE0E2EC),
    onSurfaceVariant = Color(0xFF44474F),
    outline = Color(0xFF74777F),
)

@Composable
private fun VmaTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkScheme else LightScheme,
        shapes = MaterialTheme.shapes.copy(
            extraLarge = RoundedCornerShape(28.dp),
            large = RoundedCornerShape(20.dp),
        ),
        content = content,
    )
}
