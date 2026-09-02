package dev.vma.sensor

import java.util.ArrayDeque

/** One captured frame awaiting delivery to the desktop. */
data class PendingFrame(
    val seq: Long,
    val tsDeviceUtc: String,
    val jpeg: ByteArray,
    val width: Int,
    val height: Int,
    val gps: Map<String, Any?>?,
)

/**
 * Bounded in-memory buffer for frames captured while the connection is down.
 * Latest-wins: when full, the oldest buffered frame is dropped (a stale backlog
 * is worthless to the desktop; current frames matter). Frames already acked by
 * the server are never re-sent — on reconnect we replay from lastAckedSeq+1
 * and the server additionally dedups by seq.
 */
class FrameBuffer(private val capacity: Int = 30) {
    private val frames = ArrayDeque<PendingFrame>()
    private var totalBuffered = 0L
    private var totalDropped = 0L

    @Synchronized
    fun add(frame: PendingFrame) {
        if (frames.size >= capacity) {
            frames.pollFirst()
            totalDropped++
        }
        frames.addLast(frame)
        totalBuffered++
    }

    @Synchronized
    fun pendingAfter(lastAckedSeq: Long): List<PendingFrame> =
        frames.filter { it.seq > lastAckedSeq }

    @Synchronized
    fun trimUpTo(ackedSeq: Long) {
        while (true) {
            val first = frames.peekFirst() ?: break
            if (first.seq > ackedSeq) break
            frames.pollFirst()
        }
    }

    @Synchronized
    fun clear() = frames.clear()

    @Synchronized
    fun size(): Int = frames.size

    fun stats(): String = "buffered=${size()} total=$totalBuffered dropped=$totalDropped"
}
