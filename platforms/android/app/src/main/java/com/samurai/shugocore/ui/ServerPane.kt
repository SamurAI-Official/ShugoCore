// ServerPane.kt — SERVER tab: inference, health indicators, server stats,
// model library (catalog download / sideload / select) and backup-server URL.
package com.samurai.shugocore.ui

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import com.samurai.shugocore.inference.CatalogModel
import com.samurai.shugocore.inference.ModelCatalog
import com.samurai.shugocore.inference.ModelDownloader
import com.samurai.shugocore.runtime.ControlPlaneHost
import java.io.File

class ServerPane(
    context: Context,
    private val host: ControlPlaneHost,
    private val modelDownloader: ModelDownloader,
) : LinearLayout(context) {

    private val handler = Handler(Looper.getMainLooper())
    private val statusLine: TextView
    private val infStatusDot: TextView
    private val infStatusValue: TextView
    private val engine: TextView
    private val model: TextView
    private val quant: TextView
    private val healthDots = mutableMapOf<String, TextView>()
    private val healthValues = mutableMapOf<String, TextView>()
    private val latency: TextView
    private val requests: TextView
    private val tokens: TextView
    private val modelLine: TextView
    private val progress: ProgressBar
    private val backupUrl: EditText
    private val probeParse: TextView
    private val probeClass: TextView
    private val probeLatency: TextView
    private val probeModel: TextView
    private val probeRaw: TextView

    init {
        orientation = VERTICAL
        // Ui.pane returns (ScrollView, inner column) with the column already
        // parented inside the ScrollView — add the ScrollView, fill the column.
        val (scroll, col) = Ui.pane(context)
        addView(scroll)

        // -- Inference --------------------------------------------------------
        col.addView(Ui.section(context, "Inference"))
        val (stRow, dot, value) = Ui.statusRow(context, "Status")
        infStatusDot = dot
        infStatusValue = value
        col.addView(stRow)
        engine = col.addKv("Engine")
        model = col.addKv("Model")
        quant = col.addKv("Quantization")
        col.addKv("Location").text = "On-device"
        col.addKv("Endpoint").text = "127.0.0.1:11434"

        // -- Health -----------------------------------------------------------
        col.addView(Ui.section(context, "Health"))
        for (label in listOf("MODEL LOADED", "INFERENCE READY", "API READY", "AGENT READY")) {
            val (row, d, v) = Ui.statusRow(context, label)
            healthDots[label] = d
            healthValues[label] = v
            col.addView(row)
        }

        // -- Server -----------------------------------------------------------
        col.addView(Ui.section(context, "Server"))
        col.addKv("Port").text = "11434"
        col.addKv("Binding").text = "localhost"
        col.addKv("API").text = "Ollama-compatible"
        latency = col.addKv("Latency")
        requests = col.addKv("Requests")
        tokens = col.addKv("Tokens")

        // -- Model library ------------------------------------------------------
        col.addView(Ui.section(context, "Model library"))
        modelLine = TextView(context).apply {
            textSize = 13f; typeface = android.graphics.Typeface.MONOSPACE
            setTextColor(Ui.DIM)
        }
        col.addView(modelLine)
        progress = ProgressBar(context, null, android.R.attr.progressBarStyleHorizontal).apply {
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(
                LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT)
        }
        col.addView(progress)
        col.addView(Ui.button(context, "Change model…").apply {
            setOnClickListener { showModelsDialog() }
        })

        // -- Backup server ------------------------------------------------------
        col.addView(Ui.section(context, "Backup server"))
        col.addView(TextView(context).apply {
            textSize = 12f
            setTextColor(Ui.DIM)
            text = "Desktop server URL — also the fallback the agent uses " +
                "when the local server stops (blank = none)"
        })
        backupUrl = EditText(context).apply {
            hint = "http://192.168.x.x:8000"
            textSize = 14f
            setText(host.prefs().getString("desktop_api_url", ""))
            addTextChangedListener(object : TextWatcher {
                override fun afterTextChanged(s: Editable?) {
                    host.onBackupUrlChanged(s?.toString()?.trim() ?: "")
                }

                override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
                override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            })
        }
        col.addView(backupUrl)

        // -- MODEL TEST ---------------------------------------------------------
        col.addView(Ui.section(context, "Model test"))
        probeParse = col.addKv("Parse")
        probeClass = col.addKv("Class")
        probeLatency = col.addKv("Latency")
        probeModel = col.addKv("Model")
        probeRaw = TextView(context).apply {
            textSize = 11f
            typeface = android.graphics.Typeface.MONOSPACE
            setTextColor(Ui.DIM)
            setPadding(0, Ui.dp(context, 2), 0, 0)
        }
        col.addView(probeRaw)
        col.addView(Ui.button(context, "Run model test").apply {
            setOnClickListener { host.onModelProbeClicked() }
        })

        // -- Controls -----------------------------------------------------------
        col.addView(Ui.section(context, "Controls"))
        val row = LinearLayout(context).apply { orientation = HORIZONTAL }
        row.addView(Ui.button(context, "Start server").apply {
            setOnClickListener { host.onServerStartClicked() }
            layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
        })
        row.addView(Ui.button(context, "Stop server").apply {
            setOnClickListener { host.onServerStopClicked() }
            layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
                .apply { setMargins(Ui.dp(context, 8), 0, 0, 0) }
        })
        col.addView(row)

        statusLine = TextView(context).apply {
            textSize = 13f
            setTextColor(Ui.WARN)
            setPadding(0, Ui.dp(context, 8), 0, 0)
        }
        col.addView(statusLine)
    }

    private fun LinearLayout.addKv(key: String): TextView {
        val (row, value) = Ui.kv(context, key)
        addView(row)
        return value
    }

    fun status(msg: String) {
        statusLine.text = msg
    }

    fun bind(snap: Map<*, *>?) {
        val running = Ui.bool(snap, "server_running")
        val modelName = Ui.str(snap, "model", "")
        infStatusDot.setTextColor(if (running) Ui.OK else Ui.BAD)
        infStatusValue.text = if (running) "RUNNING" else "STOPPED"
        infStatusValue.setTextColor(if (running) Ui.OK else Ui.BAD)
        engine.text = "llama.cpp"
        model.text = modelName.ifEmpty { "—" }
        quant.text = quantOf(modelName)

        setHealth("MODEL LOADED", modelName.isNotEmpty(),
            if (modelName.isNotEmpty()) modelName else "no model")
        setHealth("INFERENCE READY", Ui.bool(snap, "inference_ready"),
            if (Ui.bool(snap, "inference_ready")) "session loaded" else "not loaded")
        setHealth("API READY", running, if (running) "127.0.0.1:11434" else "stopped")
        val agentReady = Ui.bool(snap, "agent_running") &&
            Ui.bool(Ui.sub(snap, "agent_status"), "engine_ready")
        setHealth("AGENT READY", agentReady, if (agentReady) "engine ok" else "—")

        val stats = Ui.sub(snap, "server_stats")
        latency.text = if (running) "${Ui.num(stats, "last_latency_ms")} ms" else "—"
        requests.text = Ui.num(stats, "requests").toString()
        tokens.text = Ui.num(stats, "tokens").toString()

        // MODEL TEST panel: honest failure classes, never collapsed.
        val probe = Ui.sub(snap, "model_probe")
        if (probe == null || Ui.str(probe, "error_class", "").isEmpty()) {
            probeParse.text = "—"; probeClass.text = "—"
            probeLatency.text = "—"; probeModel.text = "—"; probeRaw.text = ""
        } else {
            val parsed = Ui.bool(probe, "parse_valid")
            val noResponse = Ui.str(probe, "error_class") == "no_response"
            probeParse.text = when {
                parsed -> "VALID"
                noResponse -> "—"   // nothing came back: nothing to parse
                else -> "INVALID"
            }
            probeParse.setTextColor(when {
                parsed -> Ui.OK
                noResponse -> Ui.DIM
                else -> Ui.BAD
            })
            probeClass.text = Ui.str(probe, "error_class")
            probeClass.setTextColor(if (Ui.bool(probe, "ok")) Ui.OK else Ui.WARN)
            probeLatency.text = "${Ui.num(probe, "latency_ms")} ms"
            probeModel.text = Ui.str(probe, "model").ifEmpty { "—" }
            probeRaw.text = Ui.str(probe, "raw")
        }

        refreshModelText()
    }

    private fun setHealth(label: String, ok: Boolean, value: String) {
        healthDots[label]?.setTextColor(if (ok) Ui.OK else Ui.BAD)
        healthValues[label]?.text = value
        healthValues[label]?.setTextColor(if (ok) Ui.OK else Ui.DIM)
    }

    private fun quantOf(modelName: String): String {
        val m = Regex("Q[0-9](_K_[SML]|_[0-9])").find(modelName)
        return m?.value ?: "—"
    }

    private fun refreshModelText() {
        if (modelDownloader.isBusy()) return  // progress owns the line
        val active = host.service()?.activeModelName()
        val selected = modelDownloader.selectedModelPath()?.let { File(it).name }
        modelLine.text = when {
            active != null -> "$active (on-device, port 11434)"
            selected != null -> "$selected (downloaded — will load with server)"
            else -> "none — tap Change model to download"
        }
    }

    private fun fmtSize(bytes: Long): String {
        val mb = bytes / (1024.0 * 1024.0)
        return if (mb >= 1024.0) String.format("%.1f GB", mb / 1024.0) else "${mb.toInt()} MB"
    }

    // -- model catalog dialog (moved from the v1.8.1 MainActivity) ------------

    private fun showModelsDialog() {
        if (modelDownloader.isBusy()) {
            android.app.AlertDialog.Builder(context)
                .setTitle("Download in progress")
                .setMessage("Cancel the current model download?")
                .setPositiveButton("Cancel download") { _, _ -> modelDownloader.cancel() }
                .setNegativeButton("Keep downloading", null)
                .show()
            return
        }
        val recommended = try {
            ModelCatalog.recommendedFor(
                com.samurai.shugocore.inference.CapabilityDetector(context).detect().ramGb)
        } catch (_: Exception) { null }

        val entries = mutableListOf<Any>()   // CatalogModel or sideloaded File
        val labels = mutableListOf<String>()
        val activeName = host.service()?.activeModelName()
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
        android.app.AlertDialog.Builder(context)
            .setTitle("On-device models")
            .setItems(labels.toTypedArray()) { _, which -> onModelPicked(entries[which]) }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun onModelPicked(entry: Any) {
        when (entry) {
            is CatalogModel ->
                if (modelDownloader.isDownloaded(entry)) {
                    offerSelectOrDelete(entry.file, modelDownloader.localFile(entry))
                } else {
                    android.app.AlertDialog.Builder(context)
                        .setTitle(entry.label)
                        .setMessage(
                            "Download ${entry.file} (${fmtSize(entry.sizeBytes)}) from " +
                                "${entry.repo}? Saved to app-private storage."
                        )
                        .setPositiveButton("Download") { _, _ -> startDownload(entry) }
                        .setNegativeButton("Cancel", null)
                        .show()
                }
            is File -> offerSelectOrDelete(entry.name, entry)
        }
    }

    private fun offerSelectOrDelete(name: String, file: File) {
        android.app.AlertDialog.Builder(context)
            .setTitle(name)
            .setItems(arrayOf("Select & load on device", "Delete from device")) { _, which ->
                if (which == 0) {
                    selectAndLoad(file)
                } else {
                    modelDownloader.delete(file)
                    refreshModelText()
                    status("Deleted $name")
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun selectAndLoad(file: File) {
        modelDownloader.selectModel(file.absolutePath)
        refreshModelText()
        val svc = host.service()
        if (svc == null) {
            status("Model selected — Start server to load it")
            return
        }
        status("Loading ${file.name}…")
        svc.loadOnDeviceModel { path ->
            status(
                when {
                    path != null -> "On-device model active: ${File(path).name}"
                    else -> "Failed to load model — see logcat"
                })
        }
    }

    private fun startDownload(m: CatalogModel) {
        progress.visibility = View.VISIBLE
        progress.progress = 0
        status("Downloading ${m.label}…")
        modelDownloader.download(
            m,
            onProgress = { done, total -> handler.post {
                val pct = if (total > 0) (100.0 * done / total).toInt() else 0
                progress.progress = pct
                modelLine.text = "Downloading ${m.label}… $pct% " +
                    "(${done / (1024 * 1024)} / ${total / (1024 * 1024)} MB)"
            } },
            onResult = { file, error -> handler.post {
                progress.visibility = View.GONE
                when {
                    file != null -> selectAndLoad(file)
                    error != null -> { refreshModelText(); status("Download failed: $error") }
                    else -> { refreshModelText(); status("Download cancelled") }
                }
            } }
        )
    }
}
