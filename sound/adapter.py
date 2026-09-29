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
import time
from typing import Any, Callable, Dict, List

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

        frames: List[FrameStat] = []
        for entry in raw.get("frames") or []:
            if not isinstance(entry, dict):
                continue
            frames.append(FrameStat(rms=float(entry.get("rms", 0.0)),
                                    ts_ms=float(entry.get("ts_ms", 0.0)),
                                    speech_prob=entry.get("speech_prob"),
                                    main_hz=float(entry.get("main_hz", 0.0))))

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
