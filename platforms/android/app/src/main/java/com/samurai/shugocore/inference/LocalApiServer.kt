// LocalApiServer.kt - OpenAI-compatible HTTP server for local inference
package com.samurai.shugocore.inference

import android.util.Log
import com.sun.net.httpserver.HttpServer
import com.sun.net.httpserver.HttpHandler
import com.sun.net.httpserver.HttpExchange
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.util.concurrent.Executors
import java.util.concurrent.ExecutorService

/**
 * Minimal OpenAI-compatible HTTP API server.
 * Runs on 127.0.0.1:11434 (same port as Ollama) for ShugoCore compatibility.
 */
class LocalApiServer(
    private val bridge: LlamaCppBridge,
    private val port: Int = 11434
) {
    private val TAG = "LocalApiServer"
    private val executor: ExecutorService = Executors.newCachedThreadPool()
    private var server: HttpServer? = null
    
    fun start() {
        val server = HttpServer.create(
            InetSocketAddress(InetAddress.getLoopbackAddress(), port), 0
        )
        
        server.setExecutor(executor)
        server.createContext("/api/generate", GenerateHandler(bridge))
        server.createContext("/api/chat", ChatHandler(bridge))
        server.createContext("/api/tags", TagsHandler(bridge))
        server.createContext("/health", HealthHandler())
        server.createContext("/", HealthHandler()) // Catch-all
        
        this.server = server
        server.start()
        Log.i(TAG, "Local API server started on 127.0.0.1:$port")
    }
    
    fun stop() {
        server?.stop(5)
        executor.shutdown()
        Log.i(TAG, "Local API server stopped")
    }
    
    // --- Handler: /api/generate ---
    class GenerateHandler(private val bridge: LlamaCppBridge) : HttpHandler {
        override fun handle(exchange: HttpExchange) {
            val body = exchange.requestBody.bufferedReader().readText()
            // Parse JSON body (simplified - real impl would use JSON parser)
            // Return streaming response compatible with Ollama format
            
            val responseHeaders = exchange.responseHeaders
            responseHeaders.set("Content-Type", "application/json")
            responseHeaders.set("Access-Control-Allow-Origin", "*")
            
            val outputStream = exchange.sendResponse(200, responseHeaders)
            outputStream.write("""{"model":"shugocore","created_at":"2026-09-03","response":"Hello from ShugoCore!","done":true}""".toByteArray())
            outputStream.close()
        }
    }
    
    // --- Handler: /api/chat ---
    class ChatHandler(private val bridge: LlamaCppBridge) : HttpHandler {
        override fun handle(exchange: HttpExchange) {
            val body = exchange.requestBody.bufferedReader().readText()
            val responseHeaders = exchange.responseHeaders
            responseHeaders.set("Content-Type", "application/json")
            responseHeaders.set("Access-Control-Allow-Origin", "*")
            
            val outputStream = exchange.sendResponse(200, responseHeaders)
            outputStream.write("""{"id":"chatcmpl-1","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"Hello!"}}]}""".toByteArray())
            outputStream.close()
        }
    }
    
    // --- Handler: /api/tags ---
    class TagsHandler(private val bridge: LlamaCppBridge) : HttpHandler {
        override fun handle(exchange: HttpExchange) {
            val responseHeaders = exchange.responseHeaders
            responseHeaders.set("Content-Type", "application/json")
            
            val outputStream = exchange.sendResponse(200, responseHeaders)
            outputStream.write("""{"models":[{"name":"shugocore","size":0,"format":"gguf","modified_at":"2026-09-03"}]}""".toByteArray())
            outputStream.close()
        }
    }
    
    // --- Handler: /health ---
    class HealthHandler : HttpHandler {
        override fun handle(exchange: HttpExchange) {
            val responseHeaders = exchange.responseHeaders
            responseHeaders.set("Content-Type", "application/json")
            
            val outputStream = exchange.sendResponse(200, responseHeaders)
            outputStream.write("""{"status":"ok","service":"shugocore"}""".toByteArray())
            outputStream.close()
        }
    }
    
    private fun HttpExchange.sendResponse(statusCode: Int, headers: com.sun.net.httpserver.Headers): OutputStream {
        responseCode = statusCode
        headers.forEach { key, value ->
            value.forEach { v -> responseHeaders.add(key, v) }
        }
        return responseBody
    }
}