// MainActivity.kt - Minimal UI for ShugoCore Android
package com.samurai.shugocore

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.IBinder
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {
    private lateinit var startButton: Button
    private lateinit var stopButton: Button
    private lateinit var statusText: TextView
    private lateinit var memoryText: TextView
    private lateinit var serverUrlText: EditText
    
    private var service: ShugoCoreService? = null
    private var bound = false
    
    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val localBinder = binder as ShugoCoreService.LocalBinder
            service = localBinder.getService()
            bound = true
            updateStatus("Connected")
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
        
        startButton.setOnClickListener { startService() }
        stopButton.setOnClickListener { stopService() }
        
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
            leftMargin = margin
        }
        
        memoryText.layoutParams = androidx.constraintlayout.widget.ConstraintLayout.LayoutParams(
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT,
            androidx.constraintlayout.widget.ConstraintLayout.LayoutParams.WRAP_CONTENT
        ).apply {
            topToBottom = R.id.start_button
            topMargin = margin
        }
        
        layout.addView(statusText)
        layout.addView(serverUrlText)
        layout.addView(startButton)
        layout.addView(stopButton)
        layout.addView(memoryText)
        
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
    
    private fun updateStatus(status: String) {
        statusText.text = "Status: $status"
    }
}