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
    """Attribution verdict for the most recent perception window.

    v1.28 distinguished "a person I can SEE is talking to me" from "audio
    with no visible talker" (television, music, ambient noise). v1.29 adds
    the scene taxonomy the agent needs to tell "I see a person and hear
    noise" apart from "I see a person TALKING to me — giving an instruction":

      NONE                         no fresh perception evidence at all
      PERSON_PRESENT_SILENT        face fresh, but no voice and no words
      VERIFIED_PERSON              recognized words bound to a visible,
                                   attending person (not a wake-word call)
      INSTRUCTION_DIRECTED         words addressed to the agent by name
                                   (sentence-initial wake word: "Shugo…",
                                   "Hey Shugo…", "Ok Shugo…") — the
                                   strongest human-instruction evidence
      PERSON_TALKING               recognized words from a visible person
                                   with no gaze evidence either way
      UNATTRIBUTED_AUDIO           recognized words with no visible talker,
                                   or the visible person is looking away —
                                   TV dialogue, a voice in another room
      PERSON_PRESENT_AMBIENT_NOISE face fresh + voice ENERGY but no words
                                   (music / TV while a person is present)
      AMBIENT_NOISE                voice ENERGY with no words and no face

    The load-bearing distinction: VAD energy fires on music and television,
    but only real human speech produces recognized transcript text. Energy
    alone is never "someone talking to the agent".
    """
    NONE = "none"
    PERSON_PRESENT_SILENT = "person_present_silent"
    VERIFIED_PERSON = "verified_person"
    UNATTRIBUTED_AUDIO = "unattributed_audio"
    INSTRUCTION_DIRECTED = "instruction_directed"
    PERSON_TALKING = "person_talking"
    PERSON_PRESENT_AMBIENT_NOISE = "person_present_ambient_noise"
    AMBIENT_NOISE = "ambient_noise"
# Default freshness windows (ms); configurable per instance.
_DEFAULT_FACE_WINDOW_MS = 8000
_DEFAULT_SPEECH_WINDOW_MS = 15000
_DEFAULT_GAZE_WINDOW_MS = 5000
_DEFAULT_DIRECTED_SPEECH_WINDOW_MS = 10000
_DEFAULT_STALE_TIMEOUT_MS = 30000

# v1.29 wake-word vocatives (sentence-initial only). "hello shugo" is a
# greeting to a person who happens to be present — a conversation, not an
# instruction call. "Shugo …" / "Hey Shugo …" / "Ok Shugo …" address the
# agent by name and are treated as directed instruction evidence.
_WAKE_PREFIXES = ("shugo", "hey shugo", "ok shugo", "okay shugo")


def is_wake_word_call(text: str) -> bool:
    """True when the transcript begins by addressing the agent by name."""
    t = " ".join((text or "").strip().lower().split())
    return any(
        t == p or t.startswith(p + " ") or t.startswith(p + ",")
        for p in _WAKE_PREFIXES
    )


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
        # v1.28.2: temporal skip-gate for reasoning (NRR temporal-coherence
        # pattern applied to LLM invocations).  A rolling hash of the fused
        # observation lets the primary skip a minute-scale generation when
        # nothing meaningfully changed.  Bounded: last hash + last tick ts.
        self._dedup_window_s = 30.0
        self._last_obs_hash: Optional[str] = None
        self._last_obs_ts: float = 0.0
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
        # v1.29: voice ENERGY (VAD) is tracked separately from recognized
        # words — energy without words is music/TV/ambient, never speech.
        self._voice_active: bool = False
        self._voice_ts: float = 0.0
        self._last_transcript: str = ""
        self._last_transcript_is_call: bool = False

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
        """Called by AudioProvider on every final transcript.

        Recognized WORDS are the only evidence of real speech: the VAD fires
        on music and television, but the platform SpeechRecognizer only
        produces text for actual human speech (v1.29 scene classification).
        """
        now = self._clock()
        with self._lock:
            self._speech_any = bool(transcript)
            self._speech_any_ts = now
            self._last_transcript = transcript or ""
            self._last_transcript_is_call = is_wake_word_call(self._last_transcript)
            if directed is not None:
                self._speech_directed_at_agent = directed
                self._speech_directed_ts = now

    def stamp_voice(self, active: bool) -> None:
        """v1.29: voice ENERGY without (yet) recognized words — VAD fired.

        Deliberately separate from ``stamp_speech``: energy alone is music,
        television, or ambient noise and must never be treated as someone
        talking to the agent.
        """
        now = self._clock()
        with self._lock:
            self._voice_active = bool(active)
            self._voice_ts = now

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
            voice_fresh = (
                self._voice_active and _age(self._voice_ts) < self._speech_window_ms
            )
            directed_fresh = (
                self._speech_directed_at_agent is not None
                and _age(self._speech_directed_ts) < self._directed_speech_window_ms
            )
            any_signal_fresh = (
                face_fresh or speech_fresh or gaze_fresh
                or directed_fresh or voice_fresh
            )
            all_stale = (
                not face_fresh and not speech_fresh and not voice_fresh
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
                # v1.29: words with no visible talker. A wake-word call
                # ("Hey Shugo…") is someone addressing the agent — attend.
                # Unattributed words (TV dialogue, another room) are NOT
                # attention: upgrading on them let a playing video grant
                # attended-action consent (the v1.28 music-alone bug).
                if self._last_transcript_is_call:
                    conf = 0.5
                    new_state = AttentionState.ATTENDING
                else:
                    conf = 0.2
                    new_state = (
                        AttentionState.DIVERTED
                        if self._state == AttentionState.ATTENDING
                        else self._state
                    )

            if new_state != self._state:
                self._transition_to(new_state)

            self._confidence = conf
            return self._state, self._confidence

    def _classify_speech_locked(
        self,
        face_fresh: bool,
        speech_fresh: bool,
        voice_fresh: bool,
        gaze_fresh: bool,
        directed_fresh: bool,
    ) -> Tuple[SpeechSource, float]:
        """Shared scene classification. MUST be called with self._lock held.

        Precedence: wake-word call > visible-attending talker > visible
        talker (unconfirmed) > unattributed words > bare voice energy >
        silence.
        """
        gaze_now = self._gaze_toward_camera if gaze_fresh else None
        directed_now = self._speech_directed_at_agent if directed_fresh else None

        if speech_fresh:
            if self._last_transcript_is_call:
                bound = face_fresh and (gaze_now is True or directed_now is True)
                return SpeechSource.INSTRUCTION_DIRECTED, (0.95 if bound else 0.85)
            talker_is_visible = face_fresh and (
                gaze_now is True or directed_now is True
            )
            if talker_is_visible:
                return SpeechSource.VERIFIED_PERSON, 0.9
            if face_fresh:
                if gaze_now is False:
                    # Explicit negative evidence: the person in frame is
                    # looking away, so the words are probably not theirs.
                    return SpeechSource.UNATTRIBUTED_AUDIO, 0.8
                # No gaze data either way → probably the person talking.
                return SpeechSource.PERSON_TALKING, 0.7
            return SpeechSource.UNATTRIBUTED_AUDIO, 0.6
        if voice_fresh:
            # Energy without words: music / TV / ambient — never "talking".
            return (
                (SpeechSource.PERSON_PRESENT_AMBIENT_NOISE, 0.7)
                if face_fresh else (SpeechSource.AMBIENT_NOISE, 0.7)
            )
        if face_fresh:
            return SpeechSource.PERSON_PRESENT_SILENT, 0.9
        return SpeechSource.NONE, 1.0

    def speech_source(self) -> Dict[str, Any]:
        """Bind the most recent speech window to the visual channel.

        v1.29 scene taxonomy: distinguishes a wake-word instruction, a
        verified visible talker, unattributed words (TV / another room),
        and bare voice energy (music / ambient) — the "I see a person and
        hear noise" vs "I see a person giving an instruction" boundary.
        Uses the same freshness windows as ``evaluate()`` so the two views
        never disagree about what counts as evidence.
        """
        now_s = self._clock()
        with self._lock:
            def _age(sig_ts: float) -> float:
                return (now_s - sig_ts) * 1000.0

            face_fresh = self._face_detected and _age(self._face_detected_ts) < self._face_window_ms
            speech_fresh = self._speech_any and _age(self._speech_any_ts) < self._speech_window_ms
            voice_fresh = (
                self._voice_active and _age(self._voice_ts) < self._speech_window_ms
            )
            gaze_fresh = (
                self._gaze_toward_camera is not None
                and _age(self._gaze_toward_camera_ts) < self._gaze_window_ms
            )
            directed_fresh = (
                self._speech_directed_at_agent is not None
                and _age(self._speech_directed_ts) < self._directed_speech_window_ms
            )
            source, confidence = self._classify_speech_locked(
                face_fresh, speech_fresh, voice_fresh, gaze_fresh, directed_fresh)

            return {
                "source": source.value,
                "confidence": round(confidence, 2),
                "face_fresh": face_fresh,
                "speech_fresh": speech_fresh,
                "voice_fresh": voice_fresh,
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

    def should_regenerate(self, observation: Dict[str, Any],
                          window_s: Optional[float] = None) -> Tuple[bool, str]:
        """Temporal skip-gate: True when the LLM should run again.

        Returns (regenerate, reason).  The gate fires (regenerate=True) when:
          - no prior observation hash exists (first tick), or
          - the fused-observation hash differs (scene changed), or
          - the dedup window elapsed (stale reuse is unsafe), or
          - the observation carries fresh high-signal content (new speech
            transcript, new instruction, or a state-changing event).

        Otherwise (same hash inside the window, no fresh signals) the caller
        may reuse the last decision -- the NRR temporal-coherence pattern
        (frame_index / history reuse) applied to minute-scale generations.
        Fail-open: any hashing error returns (True, "hash_error").
        """
        try:
            window = (self._dedup_window_s if window_s is None
                      else max(0.0, float(window_s)))
            now = self._clock()
            current = self._observation_hash(observation)
            with self._lock:
                last_hash = self._last_obs_hash
                last_ts = self._last_obs_ts
            if last_hash is None:
                return True, "first_observation"
            if self._has_fresh_signals(observation):
                return True, "fresh_signals"
            if current != last_hash:
                return True, "observation_changed"
            if (now - last_ts) >= window:
                return True, "dedup_window_elapsed"
            return False, "observation_unchanged"
        except Exception:
            return True, "hash_error"

    def mark_regenerated(self, observation: Dict[str, Any]) -> None:
        """Record that the LLM ran for ``observation`` (closes the gate)."""
        try:
            current = self._observation_hash(observation)
        except Exception:
            return
        with self._lock:
            self._last_obs_hash = current
            self._last_obs_ts = self._clock()

    @staticmethod
    def _observation_hash(observation: Dict[str, Any]) -> str:
        """Stable hash over the decision-relevant observation slice."""
        import hashlib
        import json
        obs = observation if isinstance(observation, dict) else {}
        human = obs.get("human") if isinstance(obs.get("human"), dict) else {}
        attention = (obs.get("attention") if isinstance(obs.get("attention"), dict)
                     else {})
        scene_verdict = obs.get("scene_verdict", "")
        if not isinstance(scene_verdict, str):
            scene_verdict = str(scene_verdict)
        speech = human.get("speech_recent", "")
        if not isinstance(speech, str):
            speech = str(speech)
        slice_data = {
            "speech_source": str(human.get("speech_source", "none")),
            "face_count": human.get("face_count", 0),
            "speech_recent": speech.strip()[:200],
            "attention_state": str(attention.get("attention_state",
                                                 "unknown")),
            "scene_verdict": scene_verdict.strip()[:64],
            "mesh_peer_count": obs.get("mesh_peer_count", 0),
        }
        raw = json.dumps(slice_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _has_fresh_signals(observation: Dict[str, Any]) -> bool:
        """True when the observation carries content that must regenerate."""
        if not isinstance(observation, dict):
            return True
        human = observation.get("human")
        if isinstance(human, dict):
            speech = human.get("speech_recent", "")
            if isinstance(speech, str) and speech.strip():
                return True
            if human.get("speech_source") in ("instruction_directed",
                                              "verified_person"):
                return True
        if isinstance(observation.get("transcript"), str):
            if observation["transcript"].strip():
                return True
        events = observation.get("events")
        if isinstance(events, list) and events:
            return True
        return False

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
            voice_fresh = (
                self._voice_active and _age(self._voice_ts) < self._speech_window_ms
            )
            speech_source, source_conf = self._classify_speech_locked(
                face_fresh, speech_fresh, voice_fresh, gaze_fresh, directed_fresh)

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
                "speech_source_conf": round(source_conf, 2),
                "voice_fresh": voice_fresh,
                "face_fresh": face_fresh,
                "speech_fresh": speech_fresh,
                "gaze_fresh": gaze_fresh,
            }