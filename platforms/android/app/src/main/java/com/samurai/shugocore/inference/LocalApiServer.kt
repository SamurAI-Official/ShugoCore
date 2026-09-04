// LocalApiServer.kt — Ollama-compatible HTTP server over real llama.cpp inference.
//
// Pipeline: HTTP request -> this server -> LlamaCppBridge -> JNI -> llama.cpp
//           -> GGUF -> real tokens streamed back as NDJSON.
//
// Binds to the loopback interface ONLY (127.0.0.1) so ShugoCore's OllamaBackend
// can talk to it unmodified at http://127.0.0.1:11434.
//
// NOTE: com.sun.net.httpserver is a JDK-internal package that does NOT exist
// on the Android runtime — this is a minimal HTTP/1.1 implementation built on
// plain ServerSocket, using org.json (bundled with Android) for JSON handling.

package com.samurai.shugocore.inference

import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

class LocalApiServer(
    private val bridge: LlamaCppBridge,
    private val modelName: String = "shugocore-local",
    private val port: Int = 11434
) {
    private val TAG = "LocalApiServer"
    private val running = AtomicBoolean(false)
    private var serverSocket: ServerSocket? = null
    private val executor: ExecutorService = Executors.newCachedThreadPool()
    private val requestCounter = AtomicLong(0)

    fun start() {
        if (running.get()) return
        val ss = ServerSocket(port, 50, InetAddress.getLoopbackAddress())
        serverSocket = ss
        running.set(true)
        Thread({
            while (running.get()) {
                try {
                    val socket = ss.accept()
                    executor.submit { handleConnection(socket) }
                } catch (e: Exception) {
                    if (running.get()) Log.w(TAG, "accept failed: ${e.message}")
                }
            }
        }, "shugocore-api-accept").apply { isDaemon = true }.start()
        Log.i(TAG, "Local API server listening on 127.0.0.1:$port (model=$modelName)")
    }

    fun stop() {
        running.set(false)
        try { serverSocket?.close() } catch (_: Exception) {}
        executor.shutdownNow()
        Log.i(TAG, "Local API server stopped")
    }

    val isRunning: Boolean get() = running.get()

    // -------------------------------------------------------------------------
    // Minimal HTTP/1.1 connection handling
    // -------------------------------------------------------------------------

    private data class HttpRequest(
        val method: String,
        val path: String,
        val body: String
    )

    private fun handleConnection(socket: Socket) {
        try {
            socket.soTimeout = 120_000 // inference can take a while
            val reader = BufferedReader(InputStreamReader(socket.inputStream, Charsets.UTF_8))

            val requestLine = reader.readLine() ?: return
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val method = parts[0].uppercase()
            val rawPath = parts[1]
            val path = rawPath.substringBefore('?')

            var contentLength = 0
            while (true) {
                val line = reader.readLine() ?: break
                if (line.isEmpty()) break
                val idx = line.indexOf(':')
                if (idx > 0) {
                    val key = line.substring(0, idx).trim().lowercase(Locale.US)
                    if (key == "content-length") {
                        contentLength = line.substring(idx + 1).trim().toIntOrNull() ?: 0
                    }
                }
            }

            val body = if (contentLength > 0) {
                val buf = CharArray(contentLength)
                var read = 0
                while (read < contentLength) {
                    val n = reader.read(buf, read, contentLength - read)
                    if (n < 0) break
                    read += n
                }
                String(buf, 0, read)
            } else ""

            val request = HttpRequest(method, path, body)
            Log.d(TAG, "#${requestCounter.incrementAndGet()} $method $path")

            when {
                method == "GET" && path == "/health" -> respondJson(socket, 200, healthJson())
                method == "GET" && path == "/api/tags" -> respondJson(socket, 200, tagsJson())
                method == "POST" && path == "/api/generate" -> handleGenerate(socket, request)
                method == "POST" && path == "/api/chat" -> handleChat(socket, request)
                else -> respondJson(socket, 404, JSONObject().put("error", "not found: $path"))
            }
        } catch (e: Exception) {
            Log.w(TAG, "connection error: ${e.message}")
        } finally {
            try { socket.close() } catch (_: Exception) {}
        }
    }

    // -------------------------------------------------------------------------
    // Handlers: real generation via LlamaCppBridge
    // -------------------------------------------------------------------------

    private fun handleGenerate(socket: Socket, request: HttpRequest) {
        val req = try { JSONObject(request.body) } catch (e: Exception) {
            respondJson(socket, 400, JSONObject().put("error", "invalid JSON: ${e.message}"))
            return
        }

        val prompt = req.optString("prompt", "")
        if (prompt.isEmpty()) {
            respondJson(socket, 400, JSONObject().put("error", "missing 'prompt'"))
            return
        }

        val options = req.optJSONObject("options") ?: JSONObject()
        val temperature = options.optDouble("temperature", 0.7).toFloat()
        val topK = options.optInt("top_k", 40)
        val topP = options.optDouble("top_p", 0.9).toFloat()
        val repeatPenalty = options.optDouble("repeat_penalty", 1.1).toFloat()
        val seed = options.optInt("seed", -1)
        val maxTokens = options.optInt("num_predict", 256).coerceIn(1, 2048)
        val stream = req.optBoolean("stream", false)

        val startNs = System.nanoTime()
        var tokenCount = 0
        val collected = StringBuilder()
        var writer: BufferedWriter? = null

        if (stream) {
            writer = openNdjson(socket, 200)
        }

        val callback = object : LlamaCppBridge.CompletionCallback {
            override fun onToken(token: String) {
                tokenCount++
                if (stream && writer != null) {
                    val chunk = JSONObject()
                        .put("model", modelName)
                        .put("created_at", nowIso())
                        .put("response", token)
                        .put("done", false)
                    try {
                        writer!!.write(chunk.toString() + "\n")
                        writer!!.flush()
                    } catch (_: Exception) {}
                } else {
                    collected.append(token)
                }
            }
            override fun onComplete(stats: Map<String, Any>) {}
            override fun onError(error: String) {
                Log.w(TAG, "generation error: $error")
            }
        }

        val text = try {
            bridge.generate(
                prompt = prompt,
                maxTokens = maxTokens,
                temperature = temperature,
                topK = topK,
                topP = topP,
                repeatPenalty = repeatPenalty,
                seed = seed,
                callback = callback
            )
        } catch (e: Exception) {
            Log.e(TAG, "generate failed", e)
            if (writer != null) {
                try { writer.close() } catch (_: Exception) {}
            }
            respondJson(socket, 500, JSONObject().put("error", "inference failed: ${e.message}"))
            return
        }

        val elapsedNs = System.nanoTime() - startNs
        val responseText = if (stream) text else collected.toString()

        if (writer != null) {
            val final = JSONObject()
                .put("model", modelName)
                .put("created_at", nowIso())
                .put("response", "")
                .put("done", true)
                .put("total_duration", elapsedNs)
                .put("eval_count", tokenCount)
                .put("eval_duration", elapsedNs)
            try {
                writer.write(final.toString() + "\n")
                writer.flush()
                writer.close()
            } catch (_: Exception) {}
        } else {
            val out = JSONObject()
                .put("model", modelName)
                .put("created_at", nowIso())
                .put("response", responseText)
                .put("done", true)
                .put("total_duration", elapsedNs)
                .put("eval_count", tokenCount)
                .put("eval_duration", elapsedNs)
            respondJson(socket, 200, out)
        }
    }

    private fun handleChat(socket: Socket, request: HttpRequest) {
        val req = try { JSONObject(request.body) } catch (e: Exception) {
            respondJson(socket, 400, JSONObject().put("error", "invalid JSON: ${e.message}"))
            return
        }

        val messages = req.optJSONArray("messages")
        if (messages == null || messages.length() == 0) {
            respondJson(socket, 400, JSONObject().put("error", "missing 'messages'"))
            return
        }

        // Generic chat template (no per-model Jinja on-device):
        //   [System] ... [User] ... [Assistant] ...
        val prompt = StringBuilder()
        for (i in 0 until messages.length()) {
            val m = messages.getJSONObject(i)
            when (m.optString("role", "user")) {
                "system" -> prompt.append("[System]\n").append(m.optString("content")).append("\n\n")
                "user" -> prompt.append("[User]\n").append(m.optString("content")).append("\n\n")
                "assistant" -> prompt.append("[Assistant]\n").append(m.optString("content")).append("\n\n")
                else -> prompt.append(m.optString("content")).append("\n\n")
            }
        }
        prompt.append("[Assistant]:\n")

        val options = req.optJSONObject("options") ?: JSONObject()
        val temperature = options.optDouble("temperature", 0.7).toFloat()
        val topK = options.optInt("top_k", 40)
        val topP = options.optDouble("top_p", 0.9).toFloat()
        val repeatPenalty = options.optDouble("repeat_penalty", 1.1).toFloat()
        val seed = options.optInt("seed", -1)
        val maxTokens = options.optInt("num_predict", 256).coerceIn(1, 2048)
        val stream = req.optBoolean("stream", false)

        val startNs = System.nanoTime()
        var tokenCount = 0
        val collected = StringBuilder()
        var writer: BufferedWriter? = null
        if (stream) writer = openNdjson(socket, 200)

        val callback = object : LlamaCppBridge.CompletionCallback {
            override fun onToken(token: String) {
                tokenCount++
                if (stream && writer != null) {
                    val chunk = JSONObject()
                        .put("model", modelName)
                        .put("created_at", nowIso())
                        .put("message", JSONObject().put("role", "assistant").put("content", token))
                        .put("done", false)
                    try {
                        writer!!.write(chunk.toString() + "\n")
                        writer!!.flush()
                    } catch (_: Exception) {}
                } else {
                    collected.append(token)
                }
            }
            override fun onComplete(stats: Map<String, Any>) {}
            override fun onError(error: String) {
                Log.w(TAG, "chat error: $error")
            }
        }

        val text = try {
            bridge.generate(
                prompt = prompt.toString(),
                maxTokens = maxTokens,
                temperature = temperature,
                topK = topK,
                topP = topP,
                repeatPenalty = repeatPenalty,
                seed = seed,
                callback = callback
            )
        } catch (e: Exception) {
            Log.e(TAG, "chat failed", e)
            if (writer != null) { try { writer.close() } catch (_: Exception) {} }
            respondJson(socket, 500, JSONObject().put("error", "inference failed: ${e.message}"))
            return
        }

        val elapsedNs = System.nanoTime() - startNs
        val responseText = if (stream) text else collected.toString()

        if (writer != null) {
            val final = JSONObject()
                .put("model", modelName)
                .put("created_at", nowIso())
                .put("message", JSONObject().put("role", "assistant").put("content", ""))
                .put("done", true)
                .put("total_duration", elapsedNs)
                .put("eval_count", tokenCount)
            try {
                writer.write(final.toString() + "\n")
                writer.flush()
                writer.close()
            } catch (_: Exception) {}
        } else {
            respondJson(socket, 200, JSONObject()
                .put("model", modelName)
                .put("created_at", nowIso())
                .put("message", JSONObject().put("role", "assistant").put("content", responseText))
                .put("done", true)
                .put("total_duration", elapsedNs)
                .put("eval_count", tokenCount))
        }
    }

    // -------------------------------------------------------------------------
    // Info endpoints + response helpers
    // -------------------------------------------------------------------------

    private fun tagsJson(): JSONObject = JSONObject().put(
        "models",
        org.json.JSONArray().put(
            JSONObject()
                .put("name", modelName)
                .put("model", modelName)
                .put("format", "gguf")
                .put("size", 0)
                .put("modified_at", nowIso())
                .put("details", JSONObject().put("family", "gguf").put("format", "gguf"))
        )
    )

    private fun healthJson(): JSONObject = JSONObject()
        .put("status", if (bridge.isReady) "ok" else "no model")
        .put("service", "shugocore-local")
        .put("model", modelName)
        .put("ready", bridge.isReady)

    private fun nowIso(): String {
        val fmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
        fmt.timeZone = TimeZone.getTimeZone("UTC")
        return fmt.format(Date())
    }

    private fun respondJson(socket: Socket, code: Int, body: JSONObject) {
        val payload = body.toString().toByteArray(Charsets.UTF_8)
        val out = socket.getOutputStream()
        out.write(
            ("HTTP/1.1 $code OK\r\n" +
                "Content-Type: application/json\r\n" +
                "Content-Length: ${payload.size}\r\n" +
                "Connection: close\r\n\r\n").toByteArray(Charsets.UTF_8)
        )
        out.write(payload)
        out.flush()
    }

    /** Opens a 200 NDJSON streaming response delimited by connection close. */
    private fun openNdjson(socket: Socket, code: Int): BufferedWriter {
        val out = socket.getOutputStream()
        out.write(
            ("HTTP/1.1 $code OK\r\n" +
                "Content-Type: application/x-ndjson\r\n" +
                "Connection: close\r\n\r\n").toByteArray(Charsets.UTF_8)
        )
        out.flush()
        return BufferedWriter(OutputStreamWriter(out, Charsets.UTF_8))
    }
}
