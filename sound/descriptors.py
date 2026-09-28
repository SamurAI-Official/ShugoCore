"""Tier 0: describe the sound without a model.

Everything here is pure arithmetic over frame statistics the device already has (its
energy VAD computes per-frame RMS and maintains an adaptive noise floor). That makes
this layer free, testable and independent of every licence question -- and it is what
lets the agent say "it is loud in here" or "it just got quiet" instead of publishing a
boolean.

Inputs are `FrameStat`s (20 ms frames). Outputs are a `SoundSummary`: plain numbers and
a short vocabulary, all of which fit the interaction bus's payload budget.
"""
import math
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Dict, List, Optional

from sound.schema import (KNOWN_LEVELS, KNOWN_TRENDS, LOUD_MIN_DBFS, ONSET_RISE_DB,
                          QUIET_MAX_DBFS, SILENCE_DBFS)


@dataclass
class FrameStat:
    """One analysed frame: level, and optionally the VAD's speech probability."""

    rms: float
    ts_ms: float = 0.0
    speech_prob: Optional[float] = None
    main_hz: float = 0.0          # dominant frequency, when the device estimates one

    def dbfs(self) -> float:
        return dbfs(self.rms)


@dataclass
class SoundSummary:
    """What the recent frames add up to. Every field is a plain value."""

    frames: int = 0
    duration_ms: float = 0.0
    rms_dbfs: float = -120.0          # energy mean: one loud event does raise this
    level_dbfs: float = -120.0        # median frame: what the window mostly sounds like
    peak_dbfs: float = -120.0
    level: str = "quiet"
    trend: str = "steady"
    activity_ratio: float = 0.0      # share of frames above the silence floor
    onsets: int = 0                  # discrete events (jumps), not drift
    speech_ratio: float = 0.0        # share of frames the VAD called speech
    longest_silence_ms: float = 0.0
    main_hz: float = 0.0
    labels: List[Dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if self.level not in KNOWN_LEVELS:
            raise ValueError("unknown level " + repr(self.level))
        if self.trend not in KNOWN_TRENDS:
            raise ValueError("unknown trend " + repr(self.trend))
        for name in ("activity_ratio", "speech_ratio"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(name + " out of range: " + repr(value))

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        out: Dict[str, Any] = {
            "frames": int(self.frames),
            "duration_ms": round(float(self.duration_ms), 1),
            "rms_dbfs": round(float(self.rms_dbfs), 1),
            "level_dbfs": round(float(self.level_dbfs), 1),
            "peak_dbfs": round(float(self.peak_dbfs), 1),
            "level": self.level,
            "trend": self.trend,
            "activity_ratio": round(float(self.activity_ratio), 3),
            "onsets": int(self.onsets),
            "speech_ratio": round(float(self.speech_ratio), 3),
            "longest_silence_ms": round(float(self.longest_silence_ms), 1),
        }
        if self.main_hz:
            out["main_hz"] = round(float(self.main_hz), 1)
        if self.labels:
            out["labels"] = [dict(label) for label in self.labels]
        return out

    def describe(self) -> str:
        """One honest sentence, for the decision engine and the status line."""
        base = "sound: %s (%s, %.0f dBFS)" % (self.level, self.trend, self.level_dbfs)
        if self.onsets:
            base += ", %d onset(s)" % self.onsets
        if self.speech_ratio >= 0.5:
            base += ", speech"
        if self.labels:
            base += "; heard " + ", ".join(
                "%s %.2f" % (label.get("name"), float(label.get("confidence", 0.0)))
                for label in self.labels[:3])
        return base


def dbfs(rms: float) -> float:
    """RMS amplitude as dBFS, floored so silence is a number rather than -inf."""
    value = max(float(rms or 0.0), 1e-6)
    return 20.0 * math.log10(value)


def level_for(rms_dbfs: float) -> str:
    """Map a level onto the short vocabulary (bands live in sound.schema)."""
    if rms_dbfs < QUIET_MAX_DBFS:
        return "quiet"
    if rms_dbfs > LOUD_MIN_DBFS:
        return "loud"
    return "conversational"


def trend_for(dbfs_values: List[float], delta_db: float = 3.0) -> str:
    """Compare the ends of the window: a level that moved, or a level that held."""
    if len(dbfs_values) < 4:
        return "steady"
    third = max(1, len(dbfs_values) // 3)
    start = sum(dbfs_values[:third]) / third
    end = sum(dbfs_values[-third:]) / third
    if end - start > delta_db:
        return "rising"
    if start - end > delta_db:
        return "falling"
    return "steady"


def count_onsets(frames: List[FrameStat]) -> int:
    """Discrete events: a frame this much louder than the one before it.

    A rise, not a level: speech drifting louder is not an onset, a door closing is.
    """
    onsets = 0
    previous = None
    for frame in frames:
        current = frame.dbfs()
        if previous is not None and current - previous >= ONSET_RISE_DB:
            onsets += 1
        previous = current
    return onsets


def longest_silence_ms(frames: List[FrameStat], frame_ms: float) -> float:
    """The longest run of silent frames, in ms."""
    longest = 0.0
    run = 0.0
    for frame in frames:
        if frame.dbfs() <= SILENCE_DBFS:
            run += frame_ms
            longest = max(longest, run)
        else:
            run = 0.0
    return longest


def summarize(frames: List[FrameStat], *, frame_ms: float = 20.0,
              labels: Optional[List[Dict[str, Any]]] = None) -> SoundSummary:
    """Reduce a window of frames to a SoundSummary (Tier 0 + whatever the VAD added)."""
    frames = list(frames or [])
    summary = SoundSummary(frames=len(frames), labels=list(labels or []))
    if not frames:
        return summary
    levels = [frame.dbfs() for frame in frames]
    summary.duration_ms = len(frames) * frame_ms
    summary.peak_dbfs = max(levels)
    # Mean energy, not mean dBFS, for the window's overall energy...
    mean_power = sum(float(frame.rms or 0.0) ** 2 for frame in frames) / len(frames)
    summary.rms_dbfs = dbfs(math.sqrt(mean_power))
    # ...but the *level* is the median frame: one door slamming in an otherwise quiet
    # window is a peak, not the room's level. (Energy averaging called it "loud".)
    summary.level_dbfs = median(levels)
    summary.level = level_for(summary.level_dbfs)
    summary.trend = trend_for(levels)
    summary.onsets = count_onsets(frames)
    summary.activity_ratio = sum(1 for value in levels if value > SILENCE_DBFS) / len(frames)
    summary.longest_silence_ms = longest_silence_ms(frames, frame_ms)
    voiced = [frame.speech_prob for frame in frames if frame.speech_prob is not None]
    if voiced:
        summary.speech_ratio = sum(1 for value in voiced if value >= 0.5) / len(voiced)
    tones = [frame.main_hz for frame in frames if frame.main_hz]
    if tones:
        summary.main_hz = sum(tones) / len(tones)
    summary.validate()
    return summary


def top_labels(scores, names, *, limit: int = 5,
               floor: float = 0.10) -> List[Dict[str, Any]]:
    """Turn a class score vector into the few labels worth publishing.

    AudioSet labels are weak, so this is deliberately conservative: only scores above
    the floor, best first, bounded in number, each naming its source so a consumer can
    tell a classifier's guess from a transcript.
    """
    pairs = []
    for index, score in enumerate(scores or []):
        value = float(score)
        if value < floor:
            continue
        if isinstance(names, dict):
            name = str(names.get(index, ""))
        else:
            name = str(names[index]) if index < len(names or []) else ""
        if not name:
            continue
        pairs.append({"name": name[:48], "confidence": round(value, 3),
                      "source": "yamnet"})
    pairs.sort(key=lambda item: item["confidence"], reverse=True)
    return pairs[:limit]

