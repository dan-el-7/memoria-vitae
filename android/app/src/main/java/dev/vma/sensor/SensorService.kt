package dev.vma.sensor

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.UseCase
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.Priority
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Foreground service: owns the camera (ImageCapture + optional local Preview,
 * this phone is a remote sensor), attaches GPS, and streams JPEG frames to the
 * desktop over the VMA WebSocket protocol with adaptive rate + offline buffering.
 *
 * Local preview (spec: phone-side only): the Preview use case renders straight
 * to a phone-screen surface. Its frames NEVER enter the capture/WS/inference
 * path — no ack, no intake queue, no dependency on VLM latency.
 */
class SensorService : Service(), LifecycleOwner {

    private val lifecycleRegistry = LifecycleRegistry(this)
    override val lifecycle: Lifecycle get() = lifecycleRegistry

    private lateinit var store: DeviceStore
    private val client: OkHttpClient by lazy { SensorConnection.okHttp() }
    private var connection: SensorConnection? = null
    private val buffer = FrameBuffer(capacity = 30)
    private val seq = AtomicLong(0)
    private val running = AtomicBoolean(false)

    private var imageCapture: ImageCapture? = null
    private var cameraProvider: ProcessCameraProvider? = null
    private var cameraThread: HandlerThread? = null
    private var cameraHandler: Handler? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var fusedLocation: FusedLocationProviderClient? = null
    private var locationCallback: LocationCallback? = null

    private var lastGps: Map<String, Any?>? = null
    private var captureIntervalMs = 1000L
    private var reconnectAttempt = 0
    private var framesSent = 0L
    private var framesAcked = 0L
    private var framesSkipped = 0L
    private var framesBuffered = 0L
    private var ackTimeouts = 0L
    private var lastVerdict = "—"
    private var globalLastAcked = -1L  // survives reconnects so replays stay minimal

    private val tickHandler = Handler(Looper.getMainLooper())
    private val tickRunnable = object : Runnable {
        override fun run() {
            if (!running.get()) return
            watchdog()
            captureTick()
            tickHandler.postDelayed(this, captureIntervalMs.coerceIn(250, 10_000))
        }
    }
    private val heartbeatHandler = Handler(Looper.getMainLooper())
    private val heartbeatRunnable = object : Runnable {
        override fun run() {
            if (!running.get()) return
            connection?.let { if (it.connected) it.sendHeartbeat(lastGps) }
            heartbeatHandler.postDelayed(this, HEARTBEAT_MS)
        }
    }

    override fun onBind(intent: Intent): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_CREATE)
        store = DeviceStore(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_START)
        when (intent?.action) {
            ACTION_STOP -> {
                stopSensing()
                return START_NOT_STICKY
            }
            else -> if (!running.get()) startSensing()
        }
        return START_STICKY
    }

    @SuppressLint("MissingPermission")
    private fun startSensing() {
        startAsForeground()
        running.set(true)
        // Full epoch millis: unique across service restarts, so the desktop's
        // per-run seq dedup can never mistake a new session for a replay.
        seq.set(System.currentTimeMillis())
        acquireWakeLock()
        startLocationUpdates()
        setupCamera()
        connectLoop(first = true)
        tickHandler.postDelayed(tickRunnable, captureIntervalMs)
        heartbeatHandler.postDelayed(heartbeatRunnable, HEARTBEAT_MS)
        startAudioLoop()
        StatusBus.publish(this, "starting")
    }

    private fun stopSensing() {
        running.set(false)
        tickHandler.removeCallbacks(tickRunnable)
        heartbeatHandler.removeCallbacks(heartbeatRunnable)
        stopAudioLoop(join = false)
        connection?.close()
        connection = null
        stopLocationUpdates()
        tearDownCamera()
        releaseWakeLock()
        buffer.clear()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        StatusBus.publish(this, "idle")
    }

    // ------------------------------------------------------------ foreground

    private fun startAsForeground() {
        val channelId = "vma_sensor"
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(channelId, "Visual Memory Sensor", NotificationManager.IMPORTANCE_LOW)
        )
        val notification: Notification = Notification.Builder(this, channelId)
            .setContentTitle("VMA Sensor active")
            .setContentText(
                if (running.get() && store.continuousAudio) "Streaming camera + audio to desktop"
                else "Streaming to desktop"
            )
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 30) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA or
                ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
            // The microphone type is only added when the user opted into
            // continuous audio (off by default) and granted RECORD_AUDIO.
            if (store.continuousAudio && hasMicPermission()) {
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            }
            startForeground(NOTIFICATION_ID, notification, type)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun hasMicPermission() =
        checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED

    // ---------------------------------------------------------------- camera

    @SuppressLint("MissingPermission")
    private fun setupCamera() {
        cameraThread = HandlerThread("CameraOps").also { it.start() }
        cameraHandler = Handler(cameraThread!!.looper)
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            try {
                cameraProvider = future.get()
                imageCapture = ImageCapture.Builder()
                    .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                    .setResolutionSelector(
                        ResolutionSelector.Builder()
                            .setResolutionStrategy(
                                ResolutionStrategy(android.util.Size(1280, 960),
                                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER)
                            )
                            .build()
                    )
                    .build()
                rebindCamera()
            } catch (e: Exception) {
                Log.e(TAG, "camera bind failed", e)
                StatusBus.publish(this, "error: camera ${e.message}")
            }
        }, ContextCompat.getMainExecutor(this))
    }

    /** Rebind active use cases: ImageCapture always, Preview when a phone-screen
     *  surface provider is attached (set/unset from MainActivity). */
    fun rebindCamera() {
        val provider = cameraProvider ?: return
        val capture = imageCapture ?: return
        val useCases = ArrayList<UseCase>()
        useCases.add(capture)
        previewSurfaceProvider?.let { p ->
            val preview = Preview.Builder().build()
            preview.setSurfaceProvider(p)
            useCases.add(preview)
        }
        try {
            provider.unbindAll()
            provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, *useCases.toTypedArray())
        } catch (e: Exception) {
            Log.e(TAG, "rebind failed", e)
        }
    }

    private fun tearDownCamera() {
        try {
            cameraProvider?.unbindAll()
        } catch (_: Exception) {
        }
        imageCapture = null
        cameraProvider = null
        cameraThread?.quitSafely()
        cameraThread = null
        cameraHandler = null
    }

    // ------------------------------------------------------ continuous audio
    // Opt-in (DeviceStore.continuousAudio, default OFF): while sensing, the mic
    // is recorded in ~30s segments and each is POSTed to /api/voice/note with
    // source="continuous". The desktop transcribes locally and commits the
    // transcript as a linked observation; silent segments produce nothing and
    // no audio is ever stored. Segments keep the camera streaming untouched:
    // the loop runs on its own thread and never touches capture or the WS path.

    @Volatile private var audioLoopRunning = false
    private var audioThread: Thread? = null
    private var audioSegments = 0L

    @SuppressLint("MissingPermission")
    private fun startAudioLoop() {
        if (audioLoopRunning || !running.get()) return
        if (!store.continuousAudio || !hasMicPermission()) return
        audioLoopRunning = true
        audioThread = Thread {
            while (audioLoopRunning && running.get() && store.continuousAudio) {
                val file = File(cacheDir, "vma_audio_seg.m4a")
                val rec = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this)
                          else @Suppress("DEPRECATION") MediaRecorder()
                var recorded = false
                try {
                    rec.setAudioSource(MediaRecorder.AudioSource.MIC)
                    rec.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                    rec.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                    rec.setAudioSamplingRate(16000)
                    rec.setAudioEncodingBitRate(64000)
                    rec.setOutputFile(file.absolutePath)
                    rec.prepare()
                    rec.start()
                    recorded = true
                    var waited = 0L
                    while (waited < AUDIO_SEGMENT_MS && audioLoopRunning && running.get()) {
                        Thread.sleep(200)
                        waited += 200
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "audio segment failed: ${e.message}")
                } finally {
                    try { rec.stop() } catch (_: Exception) {}
                    try { rec.release() } catch (_: Exception) {}
                }
                if (!audioLoopRunning || !running.get() || !store.continuousAudio) {
                    file.delete() // discard the partial last segment on stop
                    break
                }
                val bytes = if (recorded && file.exists())
                    runCatching { file.readBytes() }.getOrDefault(ByteArray(0)) else ByteArray(0)
                file.delete()
                if (bytes.isEmpty()) continue
                audioSegments++
                publishStats()
                val result = CommandClient.postVoiceNote(
                    store.serverUrl, store.deviceToken, bytes, source = "continuous"
                )
                if (!result.ok) StatusBus.publish(this@SensorService, "audio: ${result.message}")
            }
            audioLoopRunning = false
            StatusBus.publish(this@SensorService, "audio loop ended")
        }.apply {
            name = "VmaAudioSegs"
            start()
        }
    }

    private fun stopAudioLoop(join: Boolean) {
        audioLoopRunning = false
        val thread = audioThread
        audioThread = null
        if (join && thread != null) {
            try { thread.join(2500) } catch (_: InterruptedException) {}
        }
    }

    /** Apply the continuous-audio preference live: re-publish the foreground
     *  service type (adds/removes microphone) and start/stop the loop. */
    private fun applyAudioSetting() {
        if (!running.get()) return
        startAsForeground()
        if (store.continuousAudio && hasMicPermission()) startAudioLoop()
        else stopAudioLoop(join = true)
    }

    // ------------------------------------------------------------------ gps

    @SuppressLint("MissingPermission")
    private fun startLocationUpdates() {
        if (checkSelfPermission(android.Manifest.permission.ACCESS_FINE_LOCATION) !=
            android.content.pm.PackageManager.PERMISSION_GRANTED
        ) return
        fusedLocation = com.google.android.gms.location.LocationServices
            .getFusedLocationProviderClient(this)
        val request = LocationRequest.Builder(Priority.PRIORITY_BALANCED_POWER_ACCURACY, 5_000L)
            .setMinUpdateIntervalMillis(2_000L)
            .build()
        val callback = object : LocationCallback() {
            override fun onLocationResult(result: LocationResult) {
                val loc = result.lastLocation ?: return
                lastGps = mapOf(
                    "lat" to loc.latitude,
                    "lon" to loc.longitude,
                    "accuracy_m" to loc.accuracy.toDouble(),
                    "speed_mps" to if (loc.hasSpeed()) loc.speed.toDouble() else null,
                    "ts" to SensorConnection.isoUtcNow(),
                )
            }
        }
        locationCallback = callback
        try {
            fusedLocation?.requestLocationUpdates(request, callback, Looper.getMainLooper())
        } catch (e: SecurityException) {
            Log.w(TAG, "location denied: ${e.message}")
        }
    }

    private fun stopLocationUpdates() {
        try {
            locationCallback?.let { fusedLocation?.removeLocationUpdates(it) }
        } catch (_: Exception) {
        }
        locationCallback = null
        fusedLocation = null
    }

    // ------------------------------------------------------------ connection

    private fun connectLoop(first: Boolean) {
        if (!running.get()) return
        val token = store.deviceToken ?: run {
            StatusBus.publish(this, "error: not paired")
            return
        }
        val online = store.mode == "online" && store.hasRelayConfig
        val url: String
        if (online) {
            url = "relay://${store.relayHost}:${store.relayPort}/${store.relayChannel}/${store.relayAttachSecret}"
        } else {
            val host = DeviceStore.normalizeUrl(store.serverUrl)
            if (host.isBlank()) {
                StatusBus.publish(this, "error: no server URL")
                return
            }
            url = "ws://$host/ws/phone"
        }
        val conn = SensorConnection(client, url, token, deviceInfo(), object :
            SensorConnection.Events {
            override fun onWelcome(runId: String?, minIntervalMs: Long, heartbeatS: Long, auth: String) {
                reconnectAttempt = 0
                val authNote = if (auth == "cr+e2e") " · end-to-end encrypted" else ""
                if (runId == null) {
                    StatusBus.publish(this@SensorService, "connected — no active run on desktop$authNote")
                } else {
                    captureIntervalMs = maxOf(minIntervalMs, 250)
                    StatusBus.publish(this@SensorService, "run $runId$authNote")
                }
                flushBuffer()
            }

            override fun onAck(seq: Long, verdict: String, recIntervalMs: Long) {
                framesAcked++
                lastVerdict = verdict
                if (seq > globalLastAcked) globalLastAcked = seq
                buffer.trimUpTo(seq)
                if (recIntervalMs > 0) captureIntervalMs = recIntervalMs.coerceIn(250, 10_000)
                flushBuffer()
                publishStats()
            }

            override fun onRunStateChanged(active: Boolean) {
                StatusBus.publish(
                    this@SensorService,
                    if (active) "run active on desktop" else "run stopped on desktop",
                )
                if (active) flushBuffer()
            }

            override fun onConnectionState(state: String) {
                StatusBus.publish(this@SensorService, "connection: $state")
                if (state.startsWith("failed") || state.startsWith("closed")) {
                    scheduleReconnect()
                }
            }

            override fun onError(message: String) {
                StatusBus.publish(this@SensorService, "server: $message")
            }
        }, initialAckedSeq = globalLastAcked, crSecret = store.crSecret)
        connection = conn
        conn.relayAttachSecretRefresh = { secret -> store.relayAttachSecret = secret }
        conn.connect()
        if (!first) flushBuffer()
    }

    private fun scheduleReconnect() {
        if (!running.get()) return
        val delay = SensorConnection.backoffMs(reconnectAttempt++)
        tickHandler.postDelayed({ connectLoop(first = false) }, delay)
        StatusBus.publish(this, "reconnecting in ${delay / 1000.0}s")
    }

    private fun flushBuffer() {
        val conn = connection ?: return
        for (frame in buffer.pendingAfter(conn.lastAckedSeq)) {
            if (!conn.sendFrame(frame)) break
        }
    }

    // ------------------------------------------------------------- capturing

    /** Escape hatch for a lost ack: never stall the stream for more than 15 s. */
    private fun watchdog() {
        val conn = connection ?: return
        if (conn.hasUnacked() && conn.unackedAgeMs() > ACK_TIMEOUT_MS) {
            conn.clearUnacked()
            ackTimeouts++
            publishStats()
        }
    }

    private fun captureTick() {
        val conn = connection
        val capture = imageCapture
        if (capture == null) return

        val onlineWithRun = conn != null && conn.connected && conn.runActive
        if (onlineWithRun && conn!!.hasUnacked()) {
            framesSkipped++ // stop-and-wait: at most one frame in flight
            publishStats()
            return
        }

        val mySeq = seq.incrementAndGet()
        val gps = lastGps
        val ts = SensorConnection.isoUtcNow()
        capture.takePicture(ContextCompat.getMainExecutor(this),
            object : ImageCapture.OnImageCapturedCallback() {
                override fun onCaptureSuccess(image: androidx.camera.core.ImageProxy) {
                    try {
                        val jpeg = image.use { compress(it) }
                        val frame = PendingFrame(mySeq, ts, jpeg, image.width, image.height, gps)
                        val sent = onlineWithRun && conn!!.sendFrame(frame)
                        if (sent) {
                            framesSent++
                        } else {
                            // Offline, no active run, or socket died mid-capture:
                            // bounded latest-wins buffer, flushed on next welcome/ack.
                            buffer.add(frame)
                            framesBuffered++
                        }
                        publishStats()
                    } catch (e: Exception) {
                        Log.e(TAG, "capture processing failed", e)
                    }
                }

                override fun onError(exception: ImageCaptureException) {
                    Log.e(TAG, "capture failed", exception)
                }
            })
    }

    /** Downscale to max side 1024 and JPEG-compress before sending (bandwidth). */
    private fun compress(image: androidx.camera.core.ImageProxy): ByteArray {
        val buffer = image.planes[0].buffer
        val bytes = ByteArray(buffer.remaining()).also { buffer.get(it) }
        var bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        val maxSide = maxOf(bitmap.width, bitmap.height)
        if (maxSide > MAX_SIDE) {
            val scale = MAX_SIDE.toFloat() / maxSide
            bitmap = Bitmap.createScaledBitmap(
                bitmap,
                (bitmap.width * scale).toInt(),
                (bitmap.height * scale).toInt(),
                true,
            )
        }
        val out = ByteArrayOutputStream()
        bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
        return out.toByteArray()
    }

    private fun publishStats() {
        val audio = if (store.continuousAudio && audioLoopRunning) " audio=on(seg=$audioSegments)" else ""
        StatusBus.publish(
            this,
            "interval=${captureIntervalMs}ms sent=$framesSent acked=$framesAcked " +
                "skipped=$framesSkipped buffered=$framesBuffered timeouts=$ackTimeouts " +
                "last=$lastVerdict ${buffer.stats()}$audio",
            isStats = true,
        )
    }

    private fun deviceInfo(): JSONObject = JSONObject()
        .put("model", Build.MODEL)
        .put("manufacturer", Build.MANUFACTURER)
        .put("android_version", Build.VERSION.RELEASE)
        .put("app_version", "0.1.0")
        .put("device_id", store.deviceId ?: "")

    private fun acquireWakeLock() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "vma:sensor").apply {
            setReferenceCounted(false)
            acquire(6 * 60 * 60 * 1000L) // 6 h ceiling; released on stop
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    override fun onDestroy() {
        previewSurfaceProvider = null
        instance = null
        stopSensing()
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_STOP)
        lifecycleRegistry.handleLifecycleEvent(Lifecycle.Event.ON_DESTROY)
        client.dispatcher.executorService.shutdown()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "VmaSensor"
        private const val NOTIFICATION_ID = 42
        private const val HEARTBEAT_MS = 30_000L
        private const val ACK_TIMEOUT_MS = 15_000L
        private const val MAX_SIDE = 1024
        private const val JPEG_QUALITY = 70
        private const val AUDIO_SEGMENT_MS = 30_000L  // whisper's optimal window
        const val ACTION_STOP = "dev.vma.sensor.STOP"

        @Volatile private var previewSurfaceProvider: Preview.SurfaceProvider? = null
        @Volatile var instance: SensorService? = null
            private set

        /** Attach/detach the phone-screen preview surface. Pass null to go
         *  back to capture-only. Independent of the inference channel. */
        fun setLocalPreview(provider: Preview.SurfaceProvider?) {
            previewSurfaceProvider = provider
            instance?.rebindCamera()
        }

        /** Live-apply the opt-in continuous-audio preference while sensing. */
        fun setContinuousAudio(enabled: Boolean) {
            instance?.applyAudioSetting()
        }

        fun start(context: Context) = context.startForegroundService(
            Intent(context, SensorService::class.java)
        )

        fun stop(context: Context) = context.startService(
            Intent(context, SensorService::class.java).setAction(ACTION_STOP)
        )
    }
}
