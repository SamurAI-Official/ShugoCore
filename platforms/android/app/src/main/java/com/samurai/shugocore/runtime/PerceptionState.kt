// PerceptionState.kt — shared liveness holders for perception providers.
// SensorCapabilityManager reads these to render HONEST stream states:
// ACTIVE means frames/data actually arrived recently, never that a
// provider merely exists.
package com.samurai.shugocore.runtime

object PerceptionState {
    /** Last camera frame actually analyzed (0 = none yet). */
    @Volatile var lastCameraFrameMs: Long = 0L

    /** Face count from the most recent analysis (-1 = not analyzed yet). */
    @Volatile var lastFaceCount: Int = -1
}
