// MainActivity.kt - Minimal UI for ShugoCore Android
package com.samurai.shugocore

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.view.View
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.samurai.shugocore.inference.CapabilityDetector
import com.samurai.shugocore.inference.CatalogModel
import com.samurai.shugocore.inference.ModelCatalog
import com.samurai.shugocore.inference.ModelDownloader
import java.io.File

class MainActivity : AppCompatActivity() {
    private lateinit var startButton: Button
    private lateinit var stopButton: Button
    private lateinit var statusText: TextView
    private lateinit var memoryText: TextView
    private lateinit var serverUrlText: EditText
    private lateinit var modelsButton: Button
    private lateinit var modelText: TextView
    private lateinit var modelProgress: ProgressBar
    private lateinit var modelDownloader: ModelDownloader
    
    private var service: ShugoCoreService? = null
    private var bound = false
    private val statusHandler = Handler(Looper.getMainLooper())
    private val refreshRunnable = object : Runnable {
        override fun run() {
            updateMemoryStatus()
            statusHandler.postDelayed(this, 1000)
        }
    }
    
    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val localBinder = binder as ShugoCoreService.LocalBinder
            service = localBinder.getService()
            bound = true
            updateStatus("Connected")
            service?.let { s -> updateStatus(s.getDeviceRecommendation()) }
            statusHandler.post(refreshRunnable)
        }
        
        override fun onServiceDisconnected(name: ComponentName?) {
            bound = false
            updateStatus("Disconnected")
        }
    }
    
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        // Create UI programmatically
        val layout = androidx.constraintlayout.widget.ConstraintLayout(this).apply {
            id = R.id.root_layout
        }
        
        statusText = TextView(this).apply {
            id = R.id.status_text
            text = "ShugoCore Agent - Ready"
            textSize = 18f
        }
        
        startButton = Button(this).apply {
            id = R.id.start_button
            text = "Start Agent"
        }
        
        stopButton = Button(this).apply {
            id = R.id.stop_button
            text = "Stop Agent"
        }
        
        memoryText = TextView(this).apply {
            id = R.id.memory_text
            text = "Memory: --"
            textSize = 14f
        }

        val prefs = getSharedPreferences("shugocore_prefs", Context.MODE_PRIVATE)
        serverUrlText = EditText(this).apply {
            id = R.id.server_url_input
            hint = "Desktop server URL (blank = on-device)"
            setText(prefs.getString("desktop_api_url", ""))
            textSize = 14f
        }
        
        modelsButton = Button(this).apply {
            id = R.id.models_button
            text = "Models…"
        }
        modelText = TextView(this).apply {
            id = R.id.model_text
            text = "Model: none"
            textSize = 14f
        }
        modelProgress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            id = R.id.model_progress
            visibility = View.GONE
        }
        modelDownloader = ModelDownloader(this)

        startButton.setOnClickListener { startService() }
        stopButton.setOnClickListener { stopService() }
        modelsButton.setOnClickListener { showModelsDialog() }
        
        // Layout constraints
        val margin = (16 * resources.displayMetrics.density).toInt()
        statusText.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            startToEnd = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.PARENT_ID
            topToTop = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.PARENT_ID
            topMargin = margin
        }
        
        serverUrlText.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            0,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            topToBottom = R.id.status_text
            topMargin = margin
            startToStart = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.PARENT_ID
            endToEnd = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.PARENT_ID
        }
        
        startButton.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            topToBottom = R.id.server_url_input
            topMargin = margin
        }
        
        stopButton.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            leftToRight = R.id.start_button
            topToBottom = R.id.server_url_input
            leftMargin = margin
        }
        
        memoryText.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            topToBottom = R.id.start_button
            topMargin = margin
        }

        modelsButton.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            topToBottom = R.id.memory_text
            topMargin = margin
        }

        modelText.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            topToBottom = R.id.models_button
            topMargin = margin
        }

        modelProgress.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            0,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            topToBottom = R.id.model_text
            topMargin = margin
            startToStart = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.PARENT_ID
            endToEnd = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.PARENT_ID
        }
        
        layout.addView(statusText)
        layout.addView(serverUrlText)
        layout.addView(startButton)
        layout.addView(stopButton)
        layout.addView(memoryText)
        layout.addView(modelsButton)
        layout.addView(modelText)
        layout.addView(modelProgress)
        
        setContentView(layout)
    }
    
    override fun onStart() {
        super.onStart()
        Intent(this, ShugoCoreService::class.java).also {
            bindService(it, connection, Context.BIND_AUTO_CREATE)
        }
    }
    
    override fun onStop() {
        super.onStop()
        statusHandler.removeCallbacks(refreshRunnable)
        if (bound) {
            unbindService(connection)
            bound = false
        }
    }
    
    private fun startService() {
        val prefs = getSharedPreferences("shugocore_prefs", Context.MODE_PRIVATE)
        prefs.edit().putString("desktop_api_url", serverUrlText.text.toString().trim())
            .apply()
        Intent(this, ShugoCoreService::class.java).also {
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                startForegroundService(it)
            } else {
                startService(it)
            }
        }
        updateStatus("Starting...")
    }
    
    private fun stopService() {
        Intent(this, ShugoCoreService::class.java).also {
            stopService(it)
        }
        updateStatus("Stopped")
    }
    
    private fun updateMemoryStatus() {
        refreshModelText()
        val svc = service
        if (svc == null) { memoryText.text = "Memory: --"; return }
        val st = svc.getAgentStatus()
        val memMb = st["memory_usage_mb"]
        val usage = if (memMb != null) "$memMb MB" else "--"
        memoryText.text = "Tick ${st["tick_count"]} | Mem $usage | Tier1 ${st["tier1_entries"]} | ${st["engine"]}"
    }

    private fun refreshModelText() {
        if (this::modelDownloader.isInitialized && modelDownloader.isBusy()) return  // progress owns the line
        val active = service?.activeModelName()
        val selected = if (this::modelDownloader.isInitialized) {
            modelDownloader.selectedModelPath()?.let { File(it).name }
        } else null
        modelText?.text = when {
            active != null -> "Model: $active (on-device, port 11434)"
            selected != null -> "Model: $selected (downloaded — will load with agent)"
            else -> "Model: none — tap Models to download"
        }
    }

    private fun fmtSize(bytes: Long): String {
        val mb = bytes / (1024.0 * 1024.0)
        return if (mb >= 1024.0) String.format("%.1f GB", mb / 1024.0) else "${mb.toInt()} MB"
    }

    private fun showModelsDialog() {
        if (modelDownloader.isBusy()) {
            AlertDialog.Builder(this)
                .setTitle("Download in progress")
                .setMessage("Cancel the current model download?")
                .setPositiveButton("Cancel download") { _, _ -> modelDownloader.cancel() }
                .setNegativeButton("Keep downloading", null)
                .show()
            return
        }
        val recommended = try {
            ModelCatalog.recommendedFor(CapabilityDetector(this).detect().ramGb)
        } catch (e: Exception) { null }

        val entries = mutableListOf<Any>()   // CatalogModel or sideloaded File
        val labels = mutableListOf<String>()
        val activeName = service?.activeModelName()
        for (m in ModelCatalog.MODELS) {
            val tags = mutableListOf<String>()
            if (m === recommended) tags.add("recommended")
            if (activeName == m.file) tags.add("ACTIVE")
            else if (modelDownloader.isDownloaded(m)) tags.add("downloaded")
            val tagStr = if (tags.isEmpty()) "" else " · " + tags.joinToString(", ")
            entries.add(m)
            labels.add("${m.label} (${m.params}, ${fmtSize(m.sizeBytes)})$tagStr")
        }
        val catalogFiles = ModelCatalog.MODELS.map { it.file }.toSet()
        for (f in modelDownloader.downloadedFiles()) {
            if (f.name !in catalogFiles) {
                entries.add(f)
                val tag = if (activeName == f.name) " · ACTIVE" else ""
                labels.add("Sideloaded: ${f.name} (${fmtSize(f.length())})$tag")
            }
        }
        AlertDialog.Builder(this)
            .setTitle("On-device models")
            .setItems(labels.toTypedArray()) { _, which -> onModelPicked(entries[which]) }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun onModelPicked(entry: Any) {
        when (entry) {
            is CatalogModel -> {
                if (modelDownloader.isDownloaded(entry)) {
                    offerSelectOrDelete(entry.file, modelDownloader.localFile(entry))
                } else {
                    AlertDialog.Builder(this)
                        .setTitle(entry.label)
                        .setMessage(
                            "Download ${entry.file} (${fmtSize(entry.sizeBytes)}) from " +
                                "${entry.repo}? Saved to app-private storage."
                        )
                        .setPositiveButton("Download") { _, _ -> startDownload(entry) }
                        .setNegativeButton("Cancel", null)
                        .show()
                }
            }
            is File -> offerSelectOrDelete(entry.name, entry)
        }
    }

    private fun offerSelectOrDelete(name: String, file: File) {
        AlertDialog.Builder(this)
            .setTitle(name)
            .setItems(arrayOf("Select & load on device", "Delete from device")) { _, which ->
                if (which == 0) {
                    selectAndLoad(file)
                } else {
                    modelDownloader.delete(file)
                    refreshModelText()
                    updateStatus("Deleted $name")
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun selectAndLoad(file: File) {
        modelDownloader.selectModel(file.absolutePath)
        refreshModelText()
        val svc = service
        if (svc == null) {
            updateStatus("Model selected — Start Agent to load it")
            return
        }
        updateStatus("Loading ${file.name}…")
        svc.loadOnDeviceModel { path ->
            if (path != null) {
                val prefs = getSharedPreferences("shugocore_prefs", Context.MODE_PRIVATE)
                val desktopUrl = prefs.getString("desktop_api_url", null)?.trim()
                if (!desktopUrl.isNullOrEmpty()) {
                    updateStatus("Loaded ${File(path).name} — note: desktop URL is set and overrides on-device")
                } else {
                    updateStatus("On-device model active: ${File(path).name}")
                }
            } else {
                updateStatus("Failed to load model — see logcat")
            }
            refreshModelText()
        }
    }

    private fun startDownload(m: CatalogModel) {
        modelProgress.visibility = View.VISIBLE
        modelProgress.progress = 0
        updateStatus("Downloading ${m.label}…")
        modelDownloader.download(
            m,
            onProgress = { done, total -> statusHandler.post {
                val pct = if (total > 0) (100.0 * done / total).toInt() else 0
                modelProgress.progress = pct
                modelText.text = "Downloading ${m.label}… $pct% " +
                    "(${done / (1024 * 1024)} / ${total / (1024 * 1024)} MB)"
            } },
            onResult = { file, error -> statusHandler.post {
                modelProgress.visibility = View.GONE
                when {
                    file != null -> selectAndLoad(file)
                    error != null -> {
                        refreshModelText()
                        updateStatus("Download failed: $error")
                    }
                    else -> {
                        refreshModelText()
                        updateStatus("Download cancelled")
                    }
                }
            } }
        )
    }

    private fun updateStatus(status: String) {
        statusText.text = "Status: $status"
    }
}