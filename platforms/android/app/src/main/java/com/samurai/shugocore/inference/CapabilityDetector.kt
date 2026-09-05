// CapabilityDetector.kt - Hardware capability detection for Android
package com.samurai.shugocore.inference

import android.os.Build
import android.util.Log
import java.io.File

/**
 * Detects device hardware capabilities for optimal inference settings.
 */
data class DeviceCapabilities(
    val soc: String,
    val supportedBackends: List<String>,
    val ramGb: Int,
    val hasNpu: Boolean,
    val hasGpu: Boolean,
    val modelBudget: String,
    val recommendedQuant: String
)

class CapabilityDetector(private val context: android.content.Context) {
    private val TAG = "CapabilityDetector"
    
    fun detect(): DeviceCapabilities {
        val soc = detectSoc()
        val ramGb = detectRam()
        val hasNpu = detectNpu()
        val hasGpu = detectGpu()
        val backends = detectSupportedBackends(soc)
        val budget = calculateModelBudget(ramGb)
        val quant = recommendQuantization(ramGb, hasNpu)
        
        Log.i(TAG, "Detected: SOC=$soc, RAM=${ramGb}GB, NPU=$hasNpu, GPU=$hasGpu")
        
        return DeviceCapabilities(
            soc = soc,
            supportedBackends = backends,
            ramGb = ramGb,
            hasNpu = hasNpu,
            hasGpu = hasGpu,
            modelBudget = budget,
            recommendedQuant = quant
        )
    }
    
    private fun detectSoc(): String {
        // Try to read from system properties
        val cpuInfo = File("/proc/cpuinfo").readText()
        return when {
            cpuInfo.contains("Snapdragon", ignoreCase = true) -> "Snapdragon"
            cpuInfo.contains("Exynos", ignoreCase = true) -> "Exynos"
            cpuInfo.contains("Tensor", ignoreCase = true) -> "Tensor"
            cpuInfo.contains("MT", ignoreCase = true) -> "MediaTek"
            else -> "Unknown (${Build.HARDWARE})"
        }
    }
    
    private fun detectRam(): Int {
        val activityManager = context.getSystemService(android.content.Context.ACTIVITY_SERVICE) as android.app.ActivityManager
        val memInfo = android.app.ActivityManager.MemoryInfo()
        activityManager.getMemoryInfo(memInfo)
        val ramBytes = memInfo.totalMem
        return (ramBytes / (1024 * 1024 * 1024)).toInt()
    }
    
    private fun detectNpu(): Boolean {
        // Qualcomm Hexagon / Snapdragon NPU heuristic. detectSoc() parses
        // /proc/cpuinfo and works on all API levels (avoids Build.SOC, which
        // is API-34-only and throws NoSuchFieldError below minSdk 28).
        return try {
            val socMatch = detectSoc().contains("Snapdragon", ignoreCase = true)
            File("/dev/vndspanu").exists() || socMatch
        } catch (e: Exception) {
            false
        }
    }
    
    private fun detectGpu(): Boolean {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            android.opengl.EGL14.eglGetDisplay(0) != null
    }
    
    private fun detectSupportedBackends(soc: String): List<String> {
        val backends = mutableListOf<String>()
        backends.add("CPU")
        if (detectGpu()) backends.add("Vulkan")
        if (detectNpu()) {
            backends.add("NPU")
        }
        return backends
    }
    
    private fun calculateModelBudget(ramGb: Int): String {
        return when {
            ramGb >= 12 -> "14B"
            ramGb >= 8 -> "8B"
            ramGb >= 6 -> "7B"
            else -> "3B"
        }
    }
    
    private fun recommendQuantization(ramGb: Int, hasNpu: Boolean): String {
        return when {
            ramGb >= 12 && hasNpu -> "Q8_0"
            ramGb >= 8 -> "Q4_K_M"
            ramGb >= 6 -> "Q3_K_M"
            else -> "Q2_K"
        }
    }
    
    companion object {
        fun getGpuLayers(soc: String): Int {
            return when (soc) {
                "Snapdragon" -> 35
                "Exynos" -> 25
                "Tensor" -> 30
                else -> 0
            }
        }
    }
}