"""\
ShugoCore attention verification layer (v1.20).

Consumes perception signals and emits an AttentionState — the verdict on
whether a human is present and attending to the agent. The decision engine
uses this verdict as an additional gate on consent-gated actions (speak,
robot_navigate, etc.).

Design doctrine (carried from the provider contract):
  - This module is PROVIDER-SIDE. It must never be imported by the decision
    core (decision_engine, subconscious, execution_layer, policy). The
    engine receives attention state only as task ``context`` data, exactly
    like device telemetry.
  - Honest signals only: every signal has a freshness window; stale signals
    decay to UNKNOWN, never to ATTENDING.
  - Graceful degradation: if no gaze provider exists, ATTENDING =
    face_detected AND speech_directed (still strict, no refinement).
  - fail-open for UNKNOWN (no evidence = allowed); fail-closed for ABSENT
    and DIVERTED (explicit negative evidence = blocked).
"""

import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class AttentionState(str, Enum):
    """Verification verdict for the current human-agent interaction."""
    UNKNOWN = "unknown"       # No fresh evidence either way
    ABSENT = "absent"         # Human not present (no face, no speech)
    ATTENDING = "attending"   # Human present AND attending to the agent
    DIVERTED = "diverted"     # Human present but NOT attending


class SpeechSource(str, Enum):
    """Attribution verdict for the most recent speech window.

    Distinguishes "a person I can SEE is talking to me" from "audio with no
    visible talker" (television, music, ambient noise, a voice from another
    room) — the visual-audio binding (v1.28).

      NONE                  no fresh speech evidence at all
      PERSON_PRESENT_SILENT face fresh, but no speech in the window
      VERIFIED_PERSON       speech fresh AND attributable to a visible,
                            attending human (face fresh + gaze toward
                            camera, or an explicit directed-speech stamp)
      UNATTRIBUTED_AUDIO    speech fresh but no visible talker (or the
                            visible person is not looking at the agent) —
                            likely TV / music / ambient voice
    """
    NONE = "none"
    PERSON_PRESENT_SILENT = "person_present_silent"
    VERIFIED_PERSON = "verified_person"
    UNATTRIBUTED_AUDIO = "unattributed_audio"
# Default freshness windows (ms); configurable per instance.
_DEFAULT_FACE_WINDOW_MS = 8000
_DEFAULT_SPEECH_WINDOW_MS = 15000
_DEFAULT_GAZE_WINDOW_MS = 5000
_DEFAULT_DIRECTED_SPEECH_WINDOW_MS = 10000
_DEFAULT_STALE_TIMEOUT_MS = 30000


class AttentionLayer:
    """State machine that fuses perception signals into an AttentionState.

    Thread-safe (single RLock). Designed to be polled every tick from the
    agent shell; the state machine transitions are deterministic given the
    current signal snapshot.
    """

    def __init__(
        self,
        clock: Optional[callable] = None,
        face_window_ms: int = _DEFAULT_FACE_WINDOW_MS,
        speech_window_ms: int = _DEFAULT_SPEECH_WINDOW_MS,
        gaze_window_ms: int = _DEFAULT_GAZE_WINDOW_MS,
        directed_speech_window_ms: int = _DEFAULT_DIRECTED_SPEECH_WINDOW_MS,
        stale_timeout_ms: int = _DEFAULT_STALE_TIMEOUT_MS,
    ):
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._face_window_ms = face_window_ms
        self._speech_window_ms = speech_window_ms
        self._gaze_window_ms = gaze_window_ms
        self._directed_speech_window_ms = directed_speech_window_ms
        self._stale_timeout_ms = stale_timeout_ms

        # Signal holders (stamped by the provider side)
        self._face_detected: bool = False
        self._face_detected_ts: float = 0.0
        self._gaze_toward_camera: Optional[bool] = None
        self._gaze_toward_camera_ts: float = 0.0
        self._speech_any: bool = False
        self._speech_any_ts: float = 0.0
        self._speech_directed_at_agent: Optional[bool] = None
        self._speech_directed_ts: float = 0.0
        self._tts_speaking: bool = False

        self._state: AttentionState = AttentionState.UNKNOWN
        self._state_changed_ts: float = self._clock()
        self._confidence: float = 0.0

        # Sequence of speech observations during a contiguous attending
        # window (used by IntentExtractor).
        self._attending_speech_buffer: List[Dict[str, Any]] = []
# -- signal ingestion ----------------------------------------------------

    def stamp_face(self, face_count: int, gaze_toward_camera: Optional[bool] = None) -> None:
        """Called by VisionProvider on every camera analysis."""
        now = self._clock()
        with self._lock:
            self._face_detected = face_count > 0
            self._face_detected_ts = now
            if gaze_toward_camera is not None:
                self._gaze_toward_camera = gaze_toward_camera
                self._gaze_toward_camera_ts = now

    def stamp_speech(self, transcript: str, directed: Optional[bool] = None) -> None:
        """Called by AudioProvider on every final transcript."""
        now = self._clock()
        with self._lock:
            self._speech_any = bool(transcript)
            self._speech_any_ts = now
            if directed is not None:
                self._speech_directed_at_agent = directed
                self._speech_directed_ts = now

    def stamp_tts(self, speaking: bool) -> None:
        """Called by the agent shell (maps PerceptionState.ttsSpeaking)."""
        with self._lock:
            self._tts_speaking = speaking
# -- state machine -------------------------------------------------------

    def evaluate(self) -> Tuple[AttentionState, float]:
        """Recompute the attention state from current signals. Returns
        (state, confidence). Thread-safe; idempotent."""
        now_s = self._clock()
        with self._lock:
            def _age(sig_ts: float) -> float:
                return (now_s - sig_ts) * 1000.0

            face_fresh = self._face_detected and _age(self._face_detected_ts) < self._face_window_ms
            gaze_fresh = (
                self._gaze_toward_camera is not None
                and _age(self._gaze_toward_camera_ts) < self._gaze_window_ms
            )
            speech_fresh = self._speech_any and _age(self._speech_any_ts) < self._speech_window_ms
            directed_fresh = (
                self._speech_directed_at_agent is not None
                and _age(self._speech_directed_ts) < self._directed_speech_window_ms
            )
            any_signal_fresh = face_fresh or speech_fresh or gaze_fresh or directed_fresh
            all_stale = (
                not face_fresh and not speech_fresh
                and not gaze_fresh and not directed_fresh
            )

            if all_stale and _age(self._state_changed_ts) > self._stale_timeout_ms:
                self._transition_to(AttentionState.UNKNOWN)

            new_state = self._state
            conf = 0.0

            if not any_signal_fresh:
                if self._state != AttentionState.UNKNOWN:
                    new_state = AttentionState.UNKNOWN
            elif face_fresh:
                directed_now = (
                    self._speech_directed_at_agent if directed_fresh else None
                )
                gaze_now = (
                    self._gaze_toward_camera if gaze_fresh else None
                )

                attending_signal = (gaze_now is True) or (directed_now is True)
                diverted_signal = (gaze_now is False) and speech_fresh

                if attending_signal:
                    signals = 1  # face
                    if gaze_now is True:
                        signals += 1
                    if directed_now is True:
                        signals += 1
                    conf = min(1.0, signals / 3.0)
                    new_state = AttentionState.ATTENDING
                elif diverted_signal:
                    conf = 0.4
                    new_state = AttentionState.DIVERTED
                elif self._state == AttentionState.ATTENDING:
                    new_state = AttentionState.DIVERTED
                    conf = 0.3
                else:
                    conf = 0.3
                    new_state = AttentionState.ATTENDING
            elif speech_fresh and not face_fresh:
                conf = 0.3
                new_state = AttentionState.ATTENDING

            if new_state != self._state:
                self._transition_to(new_state)

            self._confidence = conf
            return self._state, self._confidence

    def speech_source(self) -> Dict[str, Any]:
        """Bind the most recent speech window to the visual channel.

        Returns a verdict distinguishing a visible-and-attending talker from
        unattributed audio (TV / music / ambient) — v1.28 visual-audio
        binding. Uses the same freshness windows as ``evaluate()`` so the two
        views never disagree about what counts as evidence.
        """
        now_s = self._clock()
        with self._lock:
            def _age(sig_ts: float) -> float:
                return (now_s - sig_ts) * 1000.0

            face_fresh = self._face_detected and _age(self._face_detected_ts) < self._face_window_ms
            speech_fresh = self._speech_any and _age(self._speech_any_ts) < self._speech_window_ms
            gaze_fresh = (
                self._gaze_toward_camera is not None
                and _age(self._gaze_toward_camera_ts) < self._gaze_window_ms
            )
            directed_fresh = (
                self._speech_directed_at_agent is not None
                and _age(self._speech_directed_ts) < self._directed_speech_window_ms
            )

            if not speech_fresh:
                source = SpeechSource.PERSON_PRESENT_SILENT if face_fresh else SpeechSource.NONE
                confidence = 0.9 if face_fresh else 1.0
            else:
                gaze_now = self._gaze_toward_camera if gaze_fresh else None
                directed_now = self._speech_directed_at_agent if directed_fresh else None
                talker_is_visible = face_fresh and (
                    gaze_now is True or directed_now is True
                )
                if talker_is_visible:
                    source = SpeechSource.VERIFIED_PERSON
                    confidence = 0.9
                else:
                    # Speech is fresh but no visible/attending talker — the
                    # audio is not bound to the person in frame (TV, music,
                    # ambient voice, or a turn away from the camera).
                    source = SpeechSource.UNATTRIBUTED_AUDIO
                    confidence = 0.8 if face_fresh else 0.6

            return {
                "source": source.value,
                "confidence": round(confidence, 2),
                "face_fresh": face_fresh,
                "speech_fresh": speech_fresh,
                "gaze_toward_camera": self._gaze_toward_camera if gaze_fresh else None,
            }

    def _transition_to(self, new_state: AttentionState) -> None:
        self._state = new_state
        self._state_changed_ts = self._clock()
        if new_state != AttentionState.ATTENDING:
            self._attending_speech_buffer.clear()
# -- intent extraction ---------------------------------------------------

    def record_directed_speech(self, transcript: str, confidence: float) -> None:
        """Buffer a speech observation during an attending window."""
        with self._lock:
            if self._state == AttentionState.ATTENDING:
                self._attending_speech_buffer.append({
                    "transcript": transcript,
                    "confidence": confidence,
                    "ts": self._clock(),
                })

    def extract_intent(self) -> Dict[str, Any]:
        """Lightweight subject extraction from the attending-speech buffer."""
        with self._lock:
            if not self._attending_speech_buffer:
                return {"intent_subject": None, "intent_verb": None, "intent_window_s": 0.0}

            latest = self._attending_speech_buffer[-1]["transcript"]
            window_start = self._attending_speech_buffer[0]["ts"]
            window_s = round(self._clock() - window_start, 1)

            words = latest.strip().lower().split()
            subject = None
            verb = None
            if len(words) >= 3:
                verb = words[1] if words[0] in ("can", "please", "i", "shugo") else words[0]
                subject = words[-1]
            return {
                "intent_subject": subject,
                "intent_verb": verb,
                "intent_window_s": window_s,
            }

    # -- read-only snapshot --------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Thread-safe state snapshot for logging / telemetry."""
        with self._lock:
            now_s = self._clock()
            def _age(sig_ts: float) -> float:
                return (now_s - sig_ts) * 1000.0

            face_fresh = (self._face_detected
                          and _age(self._face_detected_ts) < self._face_window_ms)
            speech_fresh = (self._speech_any
                            and _age(self._speech_any_ts) < self._speech_window_ms)
            gaze_fresh = (
                self._gaze_toward_camera is not None
                and _age(self._gaze_toward_camera_ts) < self._gaze_window_ms
            )
            directed_fresh = (
                self._speech_directed_at_agent is not None
                and _age(self._speech_directed_ts) < self._directed_speech_window_ms
            )
            speech_source = SpeechSource.NONE
            if speech_fresh:
                gaze_now = self._gaze_toward_camera if gaze_fresh else None
                directed_now = self._speech_directed_at_agent if directed_fresh else None
                if face_fresh and (gaze_now is True or directed_now is True):
                    speech_source = SpeechSource.VERIFIED_PERSON
                else:
                    speech_source = SpeechSource.UNATTRIBUTED_AUDIO
            elif face_fresh:
                speech_source = SpeechSource.PERSON_PRESENT_SILENT

            return {
                "state": self._state.value,
                "confidence": round(self._confidence, 2),
                "face_detected": self._face_detected,
                "gaze_toward_camera": self._gaze_toward_camera,
                "speech_directed_at_agent": self._speech_directed_at_agent,
                "tts_speaking": self._tts_speaking,
                "state_changed_ts": self._state_changed_ts,
                "attending_speech_count": len(self._attending_speech_buffer),
                "speech_source": speech_source.value,
                "speech_source_conf": round(
                    0.9 if speech_source == SpeechSource.VERIFIED_PERSON
                    else (0.8 if speech_source == SpeechSource.UNATTRIBUTED_AUDIO
                          else (0.9 if speech_source == SpeechSource.PERSON_PRESENT_SILENT
                                else 1.0)), 2),
                "face_fresh": face_fresh,
                "speech_fresh": speech_fresh,
                "gaze_fresh": gaze_fresh,
            }