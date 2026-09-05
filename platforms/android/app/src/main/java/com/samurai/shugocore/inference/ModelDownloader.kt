// ModelDownloader.kt — Curated GGUF catalog + resumable HTTP downloader so
// on-device llama.cpp inference works without adb pushes.
//
// Downloads land in filesDir/models — the same directory
// ShugoCoreService.findModelFile() scans — so a finished download is
// immediately selectable as the on-device model.
//
// Catalog: bartowski's GGUF quantizations (ungated HF repos, verified
// reachable without auth); sizes are the exact LFS byte counts for progress
// and integrity checks.

package com.samurai.shugocore.inference

import android.content.Context
import android.util.Log
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.atomic.AtomicBoolean

data class CatalogModel(
    val label: String,      // human-readable name shown in the UI
    val repo: String,       // Hugging Face repo id
    val file: String,       // .gguf filename inside the repo
    val sizeBytes: Long,    // exact file size on HF
    val params: String      // parameter count label, e.g. "1.5B"
) {
    val downloadUrl: String
        get() = "https://huggingface.co/$repo/resolve/main/$file"
}

object ModelCatalog {
    val MODELS = listOf(
        CatalogModel("Qwen2.5 0.5B Instruct", "bartowski/Qwen2.5-0.5B-Instruct-GGUF",
            "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf", 397808192L, "0.5B"),
        CatalogModel("Llama 3.2 1B Instruct", "bartowski/Llama-3.2-1B-Instruct-GGUF",
            "Llama-3.2-1B-Instruct-Q4_K_M.gguf", 807694464L, "1B"),
        CatalogModel("Qwen2.5 1.5B Instruct", "bartowski/Qwen2.5-1.5B-Instruct-GGUF",
            "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf", 986048768L, "1.5B"),
        CatalogModel("SmolLM2 1.7B Instruct", "bartowski/SmolLM2-1.7B-Instruct-GGUF",
            "SmolLM2-1.7B-Instruct-Q4_K_M.gguf", 1055609824L, "1.7B"),
        CatalogModel("Qwen2.5 3B Instruct", "bartowski/Qwen2.5-3B-Instruct-GGUF",
            "Qwen2.5-3B-Instruct-Q4_K_M.gguf", 1929903264L, "3B")
    )

    /** Best default pick for a device with [ramGb] RAM (CapabilityDetector tiers). */
    fun recommendedFor(ramGb: Int): CatalogModel = when {
        ramGb >= 8 -> MODELS[4]   // 3B fits an 8 GB+ device
        ramGb >= 4 -> MODELS[2]   // 1.5B is the sweet spot for 4-8 GB
        else -> MODELS[0]         // 0.5B for smaller devices
    }
}

class ModelDownloader(private val context: Context) {
    private val TAG = "ModelDownloader"
    private val cancelled = AtomicBoolean(false)
    @Volatile private var busy = false

    companion object {
        const val PREFS = "shugocore_prefs"
        const val KEY_SELECTED = "selected_model"
        private const val MAX_RETRIES = 5
    }

    fun modelsDir(): File = File(context.filesDir, "models").apply { mkdirs() }

    fun isBusy(): Boolean = busy

    fun localFile(m: CatalogModel): File = File(modelsDir(), m.file)

    /** True when the full-size .gguf is on disk (partial .part files don't count). */
    fun isDownloaded(m: CatalogModel): Boolean =
        localFile(m).exists() && localFile(m).length() == m.sizeBytes

    /** All .gguf files present locally (catalog downloads + sideloaded pushes). */
    fun downloadedFiles(): List<File> =
        modelsDir().listFiles()?.filter { it.name.endsWith(".gguf") }?.sortedBy { it.name }
            ?: emptyList()

    fun selectedModelPath(): String? {
        val p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_SELECTED, null) ?: return null
        return if (File(p).exists()) p else null
    }

    fun selectModel(path: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY_SELECTED, path).apply()
    }

    fun delete(file: File): Boolean {
        if (selectedModelPath() == file.absolutePath) {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .remove(KEY_SELECTED).apply()
        }
        return file.delete()
    }

    fun cancel() {
        cancelled.set(true)
    }

    /**
     * Download [m] on a background thread with HTTP-Range resume. Callbacks run
     * on the download thread — callers must marshal to the main thread.
     * onResult(file, error): exactly one of the two is non-null; both are null
     * when the user cancelled.
     */
    fun download(
        m: CatalogModel,
        onProgress: (done: Long, total: Long) -> Unit,
        onResult: (file: File?, error: String?) -> Unit
    ) {
        if (busy) {
            onResult(null, "A download is already in progress")
            return
        }
        busy = true
        cancelled.set(false)
        Thread({
            var result: File? = null
            var error: String? = null
            try {
                val dest = localFile(m)
                val part = File(modelsDir(), m.file + ".part")
                var offset = if (part.exists()) part.length() else 0L
                if (offset >= m.sizeBytes) offset = 0L  // stale/oversized part: restart
                var retries = 0

                while (result == null && error == null && !cancelled.get()) {
                    val resumeFrom = offset
                    val conn = URL(m.downloadUrl).openConnection() as HttpURLConnection
                    conn.connectTimeout = 30_000
                    conn.readTimeout = 60_000
                    conn.instanceFollowRedirects = true
                    if (resumeFrom > 0L) conn.setRequestProperty("Range", "bytes=$resumeFrom-")
                    try {
                        val code = conn.responseCode
                        if (code != HttpURLConnection.HTTP_OK &&
                            code != HttpURLConnection.HTTP_PARTIAL
                        ) {
                            error = "HTTP $code while downloading ${m.file}"
                        } else {
                            result = readInto(
                                conn, part, dest, m,
                                append = code == HttpURLConnection.HTTP_PARTIAL && resumeFrom > 0L,
                                base = if (code == HttpURLConnection.HTTP_PARTIAL) resumeFrom else 0L,
                                onProgress = onProgress
                            )
                            if (result == null && !cancelled.get()) {
                                val lenAfter = if (part.exists()) part.length() else 0L
                                when {
                                    lenAfter <= resumeFrom ->
                                        error = "Server did not return any data for ${m.file}"
                                    lenAfter < m.sizeBytes -> {
                                        // Stream ended early → resume where we stopped.
                                        offset = lenAfter
                                        retries++
                                        if (retries > MAX_RETRIES) {
                                            error = "Connection kept dropping (gave up after $MAX_RETRIES retries)"
                                        } else {
                                            Log.w(TAG, "Stream ended at $lenAfter, retrying (resume)")
                                        }
                                    }
                                }
                            }
                        }
                    } finally {
                        conn.disconnect()
                    }
                }
                if (cancelled.get() && result == null) error = null  // user cancel
            } catch (e: Exception) {
                error = if (cancelled.get()) null else (e.message ?: e.toString())
                Log.e(TAG, "Download failed", e)
            } finally {
                busy = false
                onResult(result, error)
            }
        }, "shugocore-model-download").apply { isDaemon = true }.start()
    }

    /**
     * Stream the open connection into [part]; on a complete download rename it
     * to [dest]. Returns dest on success, null when incomplete or cancelled.
     */
    private fun readInto(
        conn: HttpURLConnection,
        part: File,
        dest: File,
        m: CatalogModel,
        append: Boolean,
        base: Long,
        onProgress: (done: Long, total: Long) -> Unit
    ): File? {
        if (!append && base == 0L && part.exists() && part.length() > 0L) part.delete()
        val total = base + conn.contentLengthLong
        if (total <= 0L) return null
        var done = base
        var lastEmit = 0L
        FileOutputStream(part, append).use { out ->
            conn.inputStream.use { input ->
                val buf = ByteArray(1 shl 16)
                while (!cancelled.get()) {
                    val n = input.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    done += n
                    val now = System.currentTimeMillis()
                    if (now - lastEmit > 400 || done >= total) {
                        lastEmit = now
                        onProgress(done, total)
                    }
                }
            }
        }
        if (cancelled.get()) return null
        if (part.length() != m.sizeBytes) return null  // incomplete
        if (dest.exists()) dest.delete()
        return if (part.renameTo(dest)) {
            Log.i(TAG, "Downloaded ${m.file} (${m.sizeBytes} bytes)")
            dest
        } else null
    }
}
