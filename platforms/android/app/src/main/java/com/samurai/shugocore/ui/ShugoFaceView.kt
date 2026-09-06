// ShugoFaceView.kt — v1.18 presence: Shugo's face.
//
// A deliberately abstract face (two eyes + a mouth, no cartoon) drawn with
// plain Canvas — no image assets, no dependencies, honest states only:
//   OFFLINE    dim, still           (agent not running)
//   IDLE       soft, periodic blink
//   LISTENING  eyes brighten        (mic activity is fresh — real signal)
//   THINKING   slow eye pulse       (a decision cycle is in flight)
//   SPEAKING   the mouth animates   (TTS is audibly on the speaker)
// Every state is driven by a real runtime signal (PerceptionState), never
// decoration: the face only moves when the agent is actually doing the
// thing the face shows.
package com.samurai.shugocore.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.os.SystemClock
import android.view.View

class ShugoFaceView(context: Context) : View(context) {

    enum class Mode { OFFLINE, IDLE, LISTENING, THINKING, SPEAKING }

    @Volatile var mode: Mode = Mode.OFFLINE
        set(value) {
            field = value
            postInvalidate()
        }

    private val eye = Paint(Paint.ANTI_ALIAS_FLAG)
    private val mouthFill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val mouthStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }

    init {
        // Continuous gentle animation (blink + speaking/Thinking motion) at
        // ~16 fps on a tiny view; paused when detached from the window.
        postInvalidateDelayed(ANIM_INTERVAL_MS)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val now = SystemClock.elapsedRealtime()
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0f || h <= 0f) return

        // Blink: every ~4.2 s a ~160 ms blink (0 = closed, 1 = open).
        val blinkPhase = now % BLINK_PERIOD_MS
        val blink = if (blinkPhase < BLINK_LEN_MS) {
            1f - kotlin.math.sin(
                (blinkPhase.toFloat() / BLINK_LEN_MS) * Math.PI.toFloat())
        } else 1f

        val active = mode != Mode.OFFLINE && mode != Mode.IDLE
        val eyeColor = when (mode) {
            Mode.OFFLINE -> EYE_OFF
            Mode.IDLE -> EYE_DIM
            else -> EYE_LIVE
        }
        eye.color = eyeColor
        // THINKING: slow pulse; LISTENING: slightly wider eyes.
        val pulse = if (mode == Mode.THINKING)
            1f + 0.10f * kotlin.math.sin(now / 220.0).toFloat() else 1f
        val widen = if (mode == Mode.LISTENING) 1.18f else 1f
        val eyeRx = w * 0.085f * widen
        val eyeRy = eyeRx * pulse * blink.coerceIn(0.12f, 1f)
        val eyeY = h * 0.36f
        // Two eyes; vertical blink via a scaled ellipse (rx != ry).
        canvas.save()
        canvas.translate(w * 0.30f, eyeY)
        canvas.scale(1f, eyeRy / eyeRx)
        canvas.drawCircle(0f, 0f, eyeRx, eye)
        canvas.restore()
        canvas.save()
        canvas.translate(w * 0.70f, eyeY)
        canvas.scale(1f, eyeRy / eyeRx)
        canvas.drawCircle(0f, 0f, eyeRx, eye)
        canvas.restore()

        // Mouth
        val mouthY = h * 0.68f
        val mouthColor = if (active) MOUTH_LIVE else MOUTH_DIM
        when (mode) {
            Mode.SPEAKING -> {
                // Animated "talking" mouth: height follows a smooth wave.
                val open = 0.10f + 0.14f *
                    kotlin.math.abs(kotlin.math.sin(now / 110.0)).toFloat()
                mouthFill.color = mouthColor
                canvas.save()
                canvas.translate(w * 0.5f, mouthY)
                canvas.scale(1f, open * h / (w * 0.14f))
                canvas.drawCircle(0f, 0f, w * 0.14f, mouthFill)
                canvas.restore()
            }
            Mode.LISTENING -> {
                // A small ready smile.
                mouthStroke.color = mouthColor
                mouthStroke.strokeWidth = w * 0.045f
                canvas.drawArc(
                    w * 0.36f, mouthY - w * 0.10f, w * 0.64f, mouthY + w * 0.10f,
                    20f, 140f, false, mouthStroke)
            }
            Mode.THINKING -> {
                // A flat, concentrated line.
                mouthStroke.color = mouthColor
                mouthStroke.strokeWidth = w * 0.045f
                canvas.drawLine(
                    w * 0.40f, mouthY, w * 0.60f, mouthY, mouthStroke)
            }
            else -> {
                // OFFLINE / IDLE: a soft neutral line.
                mouthStroke.color = mouthColor
                mouthStroke.strokeWidth = w * 0.040f
                canvas.drawLine(
                    w * 0.42f, mouthY, w * 0.58f, mouthY, mouthStroke)
            }
        }

        if (isAttachedToWindow) postInvalidateDelayed(ANIM_INTERVAL_MS)
    }

    companion object {
        private const val ANIM_INTERVAL_MS = 60L
        private const val BLINK_PERIOD_MS = 4_200L
        private const val BLINK_LEN_MS = 160L
        private const val EYE_OFF = 0xFF4A5060.toInt()
        private const val EYE_DIM = 0xFF9AA3BC.toInt()
        private const val EYE_LIVE = 0xFFE8ECF5.toInt()
        private const val MOUTH_DIM = 0xFF7A829A.toInt()
        private const val MOUTH_LIVE = 0xFFE94560.toInt()
    }
}