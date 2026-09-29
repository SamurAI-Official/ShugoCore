"""The worker: analyse a window, or say honestly that it cannot.

Mirrors ``nrr/adapter.py``'s ``NRRRenderWorker``: an injectable backend, a fail-closed
default, and a ``compute_caps`` block that advertises the workload only when something
can really serve it. The device's native bridge and a host's Python bridge are both just
backends here, so the contract does not change when the model moves.

A backend is any object with ``available() -> bool`` and ``analyze(descriptor) -> dict``
returning any of: ``frames`` (per-frame rms / speech probability / dominant frequency),
``labels`` (name + confidence), ``speech_prob``, ``fingerprint`` (a compact vector for
later novelty work). Tier 0 summarising, label clamping and validation happen *here*, so
a backend cannot widen the contract by being generous.
"""
import json
import time
from typing import Any, Callable, Dict, List, Optional

from sound.descriptors import FrameStat, summarize, top_labels
from sound.result import not_supported, ok_result
from sound.schema import (KNOWN_LISTEN_MODES, KNOWN_MIC_STATES, MAX_LABELS,
                          MIN_LABEL_CONFIDENCE, SAMPLE_RATE)

# The workload name a pairing manifest advertises (same idea as nrr_render).
SOUND_WORKLOAD = "sound_events"

# Why a microphone is not ours, said plainly -- because "the recognizer is using it",
# "permission was never granted" and "this device has no microphone" are three different
# facts, and none of them is "the room is quiet".
MIC_REFUSAL_REASONS = {
    "busy_speech": "the on-device speech recognizer holds the microphone",
    "denied": "microphone permission is not granted",
    "absent": "no microphone is available",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    """A number from a backend that may not be in this process, or ``default``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_probability(value: Any) -> Optional[float]:
    """A speech probability in 0..1, or None.

    Impossible values are dropped rather than clamped: clamping would invent a measurement,
    and letting one through would fail result validation later -- taking the caller's ingest
    path down, when the whole point of this method is to answer honestly instead.
    """
    if value is None:
        return None
    prob = _as_float(value, default=-1.0)
    return prob if 0.0 <= prob <= 1.0 else None


class SoundPerceptionWorker:
    """Analyse one window of audio the caller already holds."""

    def __init__(self, backend: Any = None, *, audit: Any = None,
                 max_labels: int = MAX_LABELS,
                 min_confidence: float = MIN_LABEL_CONFIDENCE,
                 listen_mode: str = "sound") -> None:
        self._backend = backend
        self._audit = audit
        self.max_labels = max(1, int(max_labels))
        self.min_confidence = min(max(float(min_confidence), 0.0), 1.0)
        self.listen_mode = str(listen_mode or "sound").strip().lower()
        if self.listen_mode not in KNOWN_LISTEN_MODES:
            # Configuration errors should be loud, not silently treated as "off".
            raise ValueError("unknown listen_mode " + repr(listen_mode))

    @property
    def mic_state(self) -> str:
        """Who holds the microphone right now (see sound.schema).

        A device backend answers this, because only it can see whether the recognizer is
        listening. A backend that does not implement it is trusted to the extent it
        already claimed to be available, and an unrecognised answer counts as absent:
        fail closed, never "quiet by default".
        """
        if self._backend is None:
            return "absent"
        reader = getattr(self._backend, "mic_state", None)
        if reader is None:
            return "available" if self.available else "absent"
        try:
            state = str(reader() or "").strip().lower()
        except Exception:
            return "absent"
        return state if state in KNOWN_MIC_STATES else "absent"

    @property
    def available(self) -> bool:
        """True only when a backend says it can serve more than a promise."""
        if self._backend is None:
            return False
        try:
            return bool(self._backend.available())
        except Exception:
            return False

    def _audit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            self._audit(event_type, payload)
        except Exception:
            pass

    def analyze(self, descriptor: Dict[str, Any]) -> Dict[str, Any]:
        """Validate, analyse, summarise. Never raises; never invents labels."""
        from sound.descriptor import SoundWindowDescriptor

        window_id = str((descriptor or {}).get("window_id", ""))
        try:
            request = SoundWindowDescriptor.from_dict(descriptor or {})
        except ValueError as exc:
            self._audit_event("sound_refused_invalid_descriptor",
                              {"window_id": window_id, "reason": str(exc)})
            return not_supported(window_id, "invalid descriptor: " + str(exc))

        if self.listen_mode == "off":
            return not_supported(request.window_id,
                                 "acoustic listening is off by configuration")

        if not self.available:
            return not_supported(request.window_id,
                                 "no audio backend: models absent or unsupported")

        state = self.mic_state
        if state != "available":
            # Never a quiet room: we were not listening, and that is a different fact.
            self._audit_event("sound_refused_mic_unavailable",
                              {"window_id": request.window_id, "mic_state": state})
            return not_supported(
                request.window_id,
                "microphone unavailable: " + MIC_REFUSAL_REASONS.get(state, state))

        try:
            raw = dict(self._backend.analyze(request.to_dict()) or {})
        except Exception as exc:
            self._audit_event("sound_refused_backend_error",
                              {"window_id": request.window_id, "reason": str(exc)})
            return not_supported(request.window_id, "backend error: " + str(exc))

        if raw.get("unavailable"):
            # A backend must be able to say "I could not measure this window". Without this,
            # its only way to report a gap would be empty frames -- which summarise as a quiet
            # room, i.e. a confident claim about audio nobody measured.
            self._audit_event("sound_refused_no_measurement",
                              {"window_id": request.window_id,
                               "reason": str(raw.get("unavailable"))[:120]})
            return not_supported(request.window_id, str(raw.get("unavailable")))

        frames: List[FrameStat] = []
        for entry in raw.get("frames") or []:
            if not isinstance(entry, dict):
                continue
            frames.append(FrameStat(rms=_as_float(entry.get("rms")),
                                    ts_ms=_as_float(entry.get("ts_ms")),
                                    speech_prob=_as_probability(entry.get("speech_prob")),
                                    main_hz=_as_float(entry.get("main_hz"))))

        labels: List[Dict[str, Any]] = []
        if request.want_labels:
            offered = [entry for entry in (raw.get("labels") or [])
                       if isinstance(entry, dict)]
            labels = top_labels(
                [entry.get("confidence", 0.0) for entry in offered],
                [str(entry.get("name", "")) for entry in offered],
                limit=request.max_labels,
                floor=max(request.min_confidence, self.min_confidence))
            labels = [label for label in labels if label["name"]][:self.max_labels]

        summary = summarize(frames, frame_ms=float(raw.get("frame_ms", 20.0)),
                            labels=labels)
        speech_prob = raw.get("speech_prob")
        if speech_prob is None and frames:
            voiced = [f.speech_prob for f in frames if f.speech_prob is not None]
            if voiced:
                speech_prob = sum(voiced) / len(voiced)

        result = ok_result(request.window_id, summary.to_dict(), labels,
                           speech_prob=speech_prob)
        self._audit_event("sound_analyzed", {
            "window_id": request.window_id, "level": summary.level,
            "onsets": summary.onsets, "labels": len(labels),
            "policy": request.policy, "ts": time.time()})
        return result

    def compute_caps(self) -> Dict[str, Any]:
        """The ``compute_caps`` block for a pairing manifest.

        Empty when the worker cannot serve: an unbacked worker must not make a node look
        like it can hear. ``execution_provider`` is whatever the backend reports, never
        assumed -- the same discipline as NRR refusing to claim NNAPI it does not append.
        """
        if self.listen_mode == "off" or not self.available or self.mic_state != "available":
            # No false advertisement: a node that cannot hear right now does not claim
            # the workload, and a manifest is where that must be visible.
            return {}
        caps: Dict[str, Any] = {"workloads": [SOUND_WORKLOAD],
                                "sample_rate": SAMPLE_RATE,
                                "listen_mode": self.listen_mode,
                                "mic_state": "available"}
        try:
            extra = dict(self._backend.capabilities() or {})
        except Exception:
            extra = {}
        for key in ("labels", "execution_provider", "speech", "models", "level"):
            if key in extra:
                caps[key] = extra[key]
        return caps

    @staticmethod
    def worker_stub() -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        """The fail-closed worker: what the agent shell keeps when nothing is backed."""
        def _stub(request: Dict[str, Any]) -> Dict[str, Any]:
            return SoundPerceptionWorker(None).analyze(request or {})
        return _stub


class AndroidSoundBackend:
    """A backend that asks the phone what it already heard.

    On Android the audio never reaches Python. SoundProvider owns the microphone, calls the
    native bridge on-device, and publishes one JSON summary per analysed window into
    PerceptionState. This backend reads that summary rather than accepting samples, so Tier 0
    summarising, label clamping and naming still happen *here*: the device reports class
    indices, Python decides names, and the vocabulary (quiet/conversational/loud) keeps one
    author.

    ``bridge`` is the object Kotlin registered via ``register_sound_analyzer`` -- a
    SoundAnalyzerBridge. It reports liveness and ownership and cannot open a microphone,
    which is what keeps exactly one owner in the process. Absent, unready, stale or
    unparseable all mean the same thing here: no measurement, said out loud.

    ``max_age_ms`` bounds freshness. A summary from minutes ago is not what the room sounds
    like now, and re-reporting it as a fresh window is the quiet lie this contract refuses.
    """

    def __init__(self, bridge: Any, *, max_age_ms: float = 5000.0,
                 class_map: Optional[Dict[int, str]] = None) -> None:
        self._bridge = bridge
        self.max_age_ms = max(0.0, float(max_age_ms))
        self._class_map = class_map

    def available(self) -> bool:
        try:
            return bool(self._bridge.isAvailable())
        except Exception:
            return False

    def mic_state(self) -> str:
        """The contract's mic states, read from the device's own arbiter.

        "sound" means this layer holds the microphone; "speech" means the recogniser does,
        so the node answers not_supported naming the owner instead of reporting silence it
        never measured. Anything else -- nobody listening, or the runtime gone -- is absent.
        """
        if not self.available():
            return "absent"
        try:
            owner = str(self._bridge.micOwner() or "").strip().lower()
        except Exception:
            return "absent"
        if owner == "sound":
            return "available"
        if owner == "speech":
            return "busy_speech"
        return "absent"

    def listen_mode(self) -> str:
        """What the device says it is listening for; an unrecognised answer stays as it is."""
        try:
            return str(self._bridge.listenMode() or "").strip().lower()
        except Exception:
            return ""

    def capabilities(self) -> Dict[str, Any]:
        """Advertise what the runtime really does, from its own capability JSON rather than
        from what a build might have preferred -- the same discipline NRR applies to NNAPI."""
        try:
            caps = json.loads(str(self._bridge.capabilitiesJson()))
        except Exception:
            return {}
        if not isinstance(caps, dict):
            return {}
        out: Dict[str, Any] = {
            "execution_provider": str(caps.get("execution_provider", "CPU")),
            "models": {"vad": bool(caps.get("vad")),
                       "classifier": bool(caps.get("classifier"))},
            "speech": bool(caps.get("vad")),
        }
        try:
            out["labels"] = int(caps.get("classes", 0))
        except (TypeError, ValueError):
            pass
        return out


    # -- the measurement -----------------------------------------------------

    def analyze(self, descriptor: Dict[str, Any]) -> Dict[str, Any]:
        """Translate the device's published window into this contract's vocabulary."""
        payload = self._latest_event()
        if payload is None:
            return {"unavailable": "the device has no fresh sound measurement"}

        frames: List[Dict[str, Any]] = []
        for entry in payload.get("frames") or []:
            if not isinstance(entry, dict):
                continue
            frames.append({"rms": _as_float(entry.get("rms")),
                           "speech_prob": _as_probability(entry.get("speech_prob")),
                           "main_hz": 0.0})  # the device does not compute one
        if not frames:
            # Frames *are* the measurement. No frames is no measurement -- never "quiet".
            return {"unavailable": "the device reported a window with no frames"}

        names = self._names()
        labels: List[Dict[str, Any]] = []
        for entry in payload.get("labels") or []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            name = names.get(index)
            if not name:
                # An index we cannot name is skipped, never guessed: a wrong name is worse
                # than a missing one, and AudioSet labels are weak to begin with.
                continue
            labels.append({"name": str(name),
                           "confidence": float(entry.get("score", 0.0))})

        speech = _as_probability(payload.get("speech_prob"))

        return {"frames": frames, "labels": labels,
                "frame_ms": float(payload.get("frame_ms", 32.0)),
                "speech_prob": speech}

    def _latest_event(self) -> Optional[Dict[str, Any]]:
        """The device's last published window, or None when there is no usable one."""
        try:
            raw = str(self._bridge.lastSoundEventJson() or "")
        except Exception:
            return None
        if not raw:
            return None
        age = self._event_age_ms()
        if age is None or age < 0 or age > self.max_age_ms:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("status", "")) not in ("ok", "level_only"):
            # run_failed / tensor_failed / read_failed: the classifier did not measure.
            # That is not a quiet room, and it must not be summarised as one.
            return None
        return payload

    def _event_age_ms(self) -> Optional[float]:
        """How old the device's last measurement is.

        A bridge that cannot say is treated as unable to be current: failing closed here
        costs a fresh-looking summary, which is the cheaper mistake of the two.
        """
        reader = getattr(self._bridge, "soundEventAgeMs", None)
        if not callable(reader):
            return None
        try:
            return float(reader())
        except Exception:
            return None

    def _names(self) -> Dict[int, str]:
        """AudioSet index -> name, resolved once, from the same map the models were checked
        against. Missing map means unnamed labels, not invented ones."""
        if self._class_map is None:
            try:
                from sound.models import load_class_map
                self._class_map = load_class_map() or {}
            except Exception:
                self._class_map = {}
        return self._class_map


def android_native_worker(analyzer_bridge: Any, *, audit: Any = None,
                          listen_mode: Optional[str] = None,
                          max_age_ms: float = 5000.0) -> Optional[SoundPerceptionWorker]:
    """Build a :class:`SoundPerceptionWorker` backed by the phone's own microphone.

    ``analyzer_bridge`` is the object the Kotlin service registered via
    ``AndroidAgent.register_sound_analyzer`` (a SoundAnalyzerBridge). The device owns
    construction -- it needs a Context and the packaged ONNX files -- so Python only ever
    receives a ready object, and this returns None when it is missing or not ready. Callers
    keep the fail-closed ``worker_stub`` in that case, exactly as
    ``nrr.adapter.android_native_worker`` does, rather than degrading silently.

    ``listen_mode`` defaults to whatever the device reports. The device is the authority on
    who holds the microphone, so asserting a mode it is not in would make the capability
    block describe a configuration that does not exist; an unrecognised answer means this
    node cannot describe its own listening honestly, and no worker is built.
    """
    if analyzer_bridge is None:
        return None
    try:
        if not analyzer_bridge.isAvailable():
            return None
    except Exception:
        return None
    backend = AndroidSoundBackend(analyzer_bridge, max_age_ms=max_age_ms)
    mode = listen_mode
    if mode is None:
        mode = backend.listen_mode()
        if mode not in KNOWN_LISTEN_MODES:
            return None
    return SoundPerceptionWorker(backend, audit=audit, listen_mode=mode)


def observation_payload(result: Dict[str, Any]) -> Dict[str, str]:
    """Flatten a result into something the interaction bus will accept.

    The bus caps payloads at 12 keys of 160 chars, which is the right budget for "what
    was heard": the shape of the sound, and the labels as one short string.
    """
    summary = dict((result or {}).get("summary") or {})
    labels = (result or {}).get("labels") or []
    heard = ", ".join("%s %.2f" % (label.get("name", ""),
                                   float(label.get("confidence", 0.0)))
                      for label in labels[:3])
    payload = {
        "level": str(summary.get("level", "quiet"))[:16],
        "trend": str(summary.get("trend", "steady"))[:16],
        "rms_dbfs": "%d" % int(round(float(summary.get("rms_dbfs", -120.0)))),
        "onsets": str(int(summary.get("onsets", 0))),
        "duration_ms": str(int(summary.get("duration_ms", 0))),
    }
    if summary.get("speech_ratio"):
        payload["speech_ratio"] = "%.2f" % float(summary["speech_ratio"])
    if heard:
        payload["heard"] = heard[:160]
    return payload
