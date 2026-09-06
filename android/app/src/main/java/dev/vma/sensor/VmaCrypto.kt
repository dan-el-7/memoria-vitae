package dev.vma.sensor

import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * v2 auth crypto shared by both sensor transports (LAN WS + relay TCP).
 *
 * Pairing hands the phone a device token AND a challenge-response secret.
 * On every connect the phone proves possession of the secret via
 * HMAC-SHA256 over both connection nonces — the secret never crosses the
 * wire again. After a successful handshake both sides derive the same
 * AES-256-GCM session key (HKDF-SHA256, matching the desktop's
 * vma.security.auth_crypto) and everything, including camera frames, is
 * sealed end-to-end. A relay operator sees only ciphertext.
 */
object VmaCrypto {

    private val DIR_P2S = byteArrayOf(0x50, 0x32, 0x53, 0x00)  // "P2S\0" phone->desktop
    private val DIR_S2P = byteArrayOf(0x53, 0x32, 0x50, 0x00)  // "S2P\0" desktop->phone

    fun nonce(): String = java.util.UUID.randomUUID().toString().replace("-", "")

    fun hmacSha256(secret: String, message: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return mac.doFinal(message.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }

    /** Desktop contract: HMAC(secret, "vma-auth-v1:<server>:<client>"). */
    fun authResponse(secret: String, serverNonce: String, clientNonce: String): String =
        hmacSha256(secret, "vma-auth-v1:$serverNonce:$clientNonce")

    /**
     * HKDF-SHA256 (RFC 5869 extract+expand) — matches cryptography's
     * HKDF(algorithm=SHA256, length=32, salt=server+client nonces,
     * info="vma-e2e-v1").derive(secret).
     */
    fun deriveSessionKey(secret: String, serverNonce: String, clientNonce: String): ByteArray {
        val prk = Mac.getInstance("HmacSHA256").apply {
            init(SecretKeySpec((serverNonce + clientNonce).toByteArray(Charsets.UTF_8), "HmacSHA256"))
        }.doFinal(secret.toByteArray(Charsets.UTF_8))
        val expand = Mac.getInstance("HmacSHA256").apply {
            init(SecretKeySpec(prk, "HmacSHA256"))
        }
        expand.update("vma-e2e-v1".toByteArray(Charsets.UTF_8))
        expand.update(byteArrayOf(0x01))
        return expand.doFinal()  // exactly 32 bytes (one SHA-256 block)
    }

    // -------------------------------------------------------- sealed frames

    class SealedSession(private val key: ByteArray) {
        private var sendCounter = 0L
        private var recvCounter = 0L

        /** Wire format: [8B BE counter][12B nonce][ciphertext+16B GCM tag]. */
        fun seal(plaintext: ByteArray, phoneToServer: Boolean): ByteArray {
            sendCounter += 1
            val dir = if (phoneToServer) DIR_P2S else DIR_S2P
            val nonce = dir + longToBytes(sendCounter)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
            val ct = cipher.doFinal(plaintext)
            return longToBytes(sendCounter) + nonce + ct
        }

        /** Returns null on any integrity/nonce failure (replay, tamper, wrong key). */
        fun unseal(blob: ByteArray, fromPhone: Boolean): ByteArray? {
            if (blob.size < 8 + 12 + 16) return null
            val counter = bytesToLong(blob, 0)
            if (counter <= recvCounter) return null            // replay/reorder
            val expectedDir = if (fromPhone) DIR_P2S else DIR_S2P
            val nonce = blob.copyOfRange(8, 20)
            if (!nonce.copyOfRange(0, 4).contentEquals(expectedDir)) return null
            return try {
                val cipher = Cipher.getInstance("AES/GCM/NoPadding")
                cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"),
                            GCMParameterSpec(128, nonce))
                val pt = cipher.doFinal(blob.copyOfRange(20, blob.size))
                recvCounter = counter
                pt
            } catch (e: Exception) {
                null
            }
        }
    }

    // ------------------------------------------------------------- helpers

    fun hexToBytes(hex: String): ByteArray {
        val out = ByteArray(hex.length / 2)
        for (i in out.indices) {
            out[i] = ((Character.digit(hex[i * 2], 16) shl 4) +
                      Character.digit(hex[i * 2 + 1], 16)).toByte()
        }
        return out
    }

    fun bytesToHex(bytes: ByteArray): String =
        bytes.joinToString("") { "%02x".format(it) }

    private fun bytesToLong(b: ByteArray, off: Int): Long {
        var v = 0L
        for (i in 0..7) v = (v shl 8) or (b[off + i].toLong() and 0xff)
        return v
    }

    private fun longToBytes(v: Long): ByteArray {
        val out = ByteArray(8)
        var x = v
        for (i in 7 downTo 0) {
            out[i] = (x and 0xff).toByte()
            x = x shr 8
        }
        return out
    }

    /** Build the binary sensor frame: [u32-LE header len][JSON header][JPEG]. */
    fun buildFramePayload(seq: Long, tsDeviceUtc: String, width: Int, height: Int,
                          jpeg: ByteArray, gps: Map<String, Any?>?): ByteArray {
        val header = JSONObject()
            .put("seq", seq)
            .put("ts_device", tsDeviceUtc)
            .put("w", width)
            .put("h", height)
        if (gps != null) header.put("gps", JSONObject(gps))
        val headerBytes = header.toString().toByteArray(Charsets.UTF_8)
        return ByteBuffer.allocate(4 + headerBytes.size + jpeg.size)
            .order(ByteOrder.LITTLE_ENDIAN)
            .putInt(headerBytes.size)
            .put(headerBytes)
            .put(jpeg)
            .array()
    }

    fun isHex(s: String): Boolean =
        s.isNotEmpty() && s.length % 2 == 0 &&
        s.all { it in '0'..'9' || it in 'a'..'f' || it in 'A'..'F' }
}
