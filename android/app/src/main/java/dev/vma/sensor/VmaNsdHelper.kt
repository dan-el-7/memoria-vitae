package dev.vma.sensor

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo

/**
 * Nearby-desktop discovery over mDNS/NSD (_vma._tcp.). The desktop
 * advertises itself (vma.security.discovery); this helper browses and
 * resolves instances into clickable "nearby desktops" for easy pairing.
 *
 * Uses only framework APIs — no new dependencies. TXT records need API 21+
 * (getAttributes) and host resolution works on all supported versions.
 * Browsing is scoped to the app's lifetime; stop() unregisters listeners.
 */
class VmaNsdHelper(context: Context) {

    data class Desktop(
        val instanceName: String,
        val desktopName: String,
        val host: String,
        val port: Int,
        val pairingLive: Boolean,
        val instanceId: String,
    )

    private val nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private var listener: NsdManager.DiscoveryListener? = null
    private val resolving = mutableSetOf<String>()

    /** Live map instance-name -> Desktop; observe by polling from the UI. */
    val found = mutableMapOf<String, Desktop>()
    var onUpdate: (() -> Unit)? = null

    fun start() {
        stop()
        found.clear()
        val l = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                listener = null
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                found.remove(serviceInfo.serviceName)
                onUpdate?.invoke()
            }
            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                if (serviceInfo.serviceType != SERVICE_TYPE) return
                resolve(serviceInfo)
            }
        }
        listener = l
        nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, l)
    }

    private fun resolve(info: NsdServiceInfo) {
        synchronized(resolving) {
            if (!resolving.add(info.serviceName)) return
        }
        nsdManager.resolveService(info, object : NsdManager.ResolveListener {
            override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                synchronized(resolving) { resolving.remove(info.serviceName) }
            }

            override fun onServiceResolved(info: NsdServiceInfo) {
                synchronized(resolving) { resolving.remove(info.serviceName) }
                val name = attr(info, "name") ?: info.serviceName
                val pairing = attr(info, "pair") == "1"
                val id = attr(info, "id") ?: info.serviceName
                val host = info.host?.hostAddress ?: return
                val desktop = Desktop(
                    instanceName = info.serviceName,
                    desktopName = name,
                    host = host,
                    port = info.port,
                    pairingLive = pairing,
                    instanceId = id,
                )
                found[info.serviceName] = desktop
                onUpdate?.invoke()
            }
        })
    }

    /** TXT attribute lookup compatible across API levels (API 21+). */
    private fun attr(info: NsdServiceInfo, key: String): String? = try {
        val attrs = info.attributes ?: return null
        val bytes = attrs[key] ?: return null
        if (bytes != null) String(bytes, Charsets.UTF_8) else null
    } catch (_: Exception) {
        null
    }

    fun stop() {
        listener?.let {
            try {
                nsdManager.stopServiceDiscovery(it)
            } catch (_: Exception) {
            }
        }
        listener = null
    }

    companion object {
        const val SERVICE_TYPE = "_vma._tcp."
    }
}
