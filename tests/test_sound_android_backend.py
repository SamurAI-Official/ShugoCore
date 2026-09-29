"""The device backend: what the phone publishes, turned into what the contract promises.

Every fixture here is a real shape -- the JSON is what SoundProvider.analyze() writes, keys
and all, and labels are resolved through the same AudioSet class map the models were verified
against. What the suite is really for is the honesty rules, because that is where a
convenient shortcut turns into a lie:

* a stale window is not "what the room sounds like now";
* a window the classifier failed on is not a quiet room;
* a window with no frames is not a measurement at all;
* a class index with no name is skipped, never guessed;
* a busy microphone is named, not reported as silence.

Together they exercise the whole chain the device relies on: PerceptionState JSON -> this
backend -> Tier 0 summary -> the result envelope -> the interaction-bus payload.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sound.adapter import (SOUND_WORKLOAD, SoundPerceptionWorker,  # noqa: E402
                           android_native_worker, observation_payload)
from sound.descriptor import describe_window  # noqa: E402
from sound.models import load_class_map  # noqa: E402
from sound.schema import KNOWN_LEVELS  # noqa: E402

CAPS = {"execution_provider": "CPU", "nnapi": False, "vad": True, "classifier": True,
        "sample_rate": 16000, "chunk_samples": 512, "window_samples": 15600,
        "classes": 521, "embedding": True, "last_ms": 12.5}


class _Bridge:
    """The SoundAnalyzerBridge surface exactly as Kotlin implements it: primitives only."""

    def __init__(self, *, available=True, mic_owner="sound", listen_mode="sound",
                 caps=None, event=None, age_ms=120.0):
        self.available = available
        self.mic_owner = mic_owner
        self.listen_mode = listen_mode
        self.caps = CAPS if caps is None else caps
        self.event = event
        self.age_ms = age_ms

    def isAvailable(self):
        return self.available

    def micOwner(self):
        return self.mic_owner

    def listenMode(self):
        return self.listen_mode

    def isListening(self):
        return self.mic_owner == "sound"

    def capabilitiesJson(self):
        return json.dumps(self.caps)

    def lastSoundEventJson(self):
        return self.event if self.event is not None else ""

    def soundEventAgeMs(self):
        return self.age_ms


def device_event(frames=None, labels=None, status="ok", frame_ms=32,
                 speech_prob=0.9, window_ms=975):
    """Exactly the JSON SoundProvider.analyze() publishes (see SoundProvider.kt)."""
    return json.dumps({
        "status": status, "classified": status == "ok", "frame_ms": frame_ms,
        "frames": [{"rms": 0.02, "speech_prob": 0.11}] if frames is None else frames,
        "labels": labels or [], "speech_prob": speech_prob, "window_ms": window_ms,
    })


def worker_for(bridge, **kwargs):
    worker = android_native_worker(bridge, **kwargs)
    assert worker is not None, "expected a worker for this bridge"
    return worker


class TestDeviceBackend(unittest.TestCase):
    def test_a_measured_quiet_window_is_quiet_not_an_error(self):
        """level_only windows exist so silence is a fact the device measured."""
        event = device_event(frames=[{"rms": 0.0004, "speech_prob": 0.01}],
                             status="level_only")
        result = worker_for(_Bridge(event=event)).analyze(describe_window("w-quiet"))
        self.assertEqual(result["status"], "ok")
        self.assertIn(result["summary"]["level"], KNOWN_LEVELS)
        self.assertEqual(result["labels"], [])

    def test_a_classified_window_carries_real_audioset_names(self):
        names = load_class_map()
        if not names:
            self.skipTest("AudioSet class map not fetched on this host")
        event = device_event(labels=[{"index": 494, "score": 0.97}])
        result = worker_for(_Bridge(event=event)).analyze(describe_window("w-494"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual([label["name"] for label in result["labels"]], [names[494]])
        self.assertAlmostEqual(result["labels"][0]["confidence"], 0.97, places=3)

    def test_the_whole_chain_reaches_the_bus_payload(self):
        """Device JSON -> backend -> summary -> result -> the 12-key bus payload."""
        event = device_event(frames=[{"rms": 0.05, "speech_prob": 0.6}] * 7,
                             labels=[{"index": 0, "score": 0.9}])
        result = worker_for(_Bridge(event=event)).analyze(describe_window("w-bus"))
        payload = observation_payload(result)
        self.assertEqual(payload["level"], result["summary"]["level"])
        self.assertIn("rms_dbfs", payload)
        self.assertIn("onsets", payload)
        self.assertTrue(all(len(value) <= 160 for value in payload.values()))
        self.assertLessEqual(len(payload), 12)


class _NoAgeBridge(_Bridge):
    """A bridge that cannot say how old its measurement is (an older runtime, say)."""

    soundEventAgeMs = None


class TestHonestyRules(unittest.TestCase):
    def test_a_stale_window_is_not_measured(self):
        event = device_event(labels=[{"index": 494, "score": 0.9}])
        result = worker_for(_Bridge(event=event, age_ms=60_000)).analyze(
            describe_window("w-old"))
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("fresh", result["reason"])
        self.assertFalse(result.get("summary"))
        self.assertFalse(result.get("labels"))

    def test_a_bridge_that_cannot_report_age_is_failed_closed(self):
        event = device_event(labels=[{"index": 494, "score": 0.9}])
        result = worker_for(_NoAgeBridge(event=event)).analyze(describe_window("w-noage"))
        self.assertEqual(result["status"], "not_supported")

    def test_a_failed_classifier_is_not_a_quiet_room(self):
        event = device_event(status="run_failed", labels=[])
        result = worker_for(_Bridge(event=event)).analyze(describe_window("w-failed"))
        self.assertEqual(result["status"], "not_supported")

    def test_a_window_with_no_frames_is_not_a_measurement(self):
        result = worker_for(_Bridge(event=device_event(frames=[]))).analyze(
            describe_window("w-noframes"))
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("frames", result["reason"])

    def test_nothing_heard_yet_is_not_a_quiet_room(self):
        result = worker_for(_Bridge(event=None, age_ms=-1.0)).analyze(
            describe_window("w-none"))
        self.assertEqual(result["status"], "not_supported")

    def test_unparseable_device_json_never_raises(self):
        result = worker_for(_Bridge(event="{not json")).analyze(describe_window("w-junk"))
        self.assertEqual(result["status"], "not_supported")

    def test_an_unnamed_index_is_skipped_and_never_guessed(self):
        event = device_event(labels=[{"index": 999999, "score": 0.9},
                                     {"index": "not-a-number", "score": 0.9}])
        result = worker_for(_Bridge(event=event)).analyze(describe_window("w-unnamed"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["labels"], [])

    def test_an_impossible_probability_is_dropped_not_clamped(self):
        event = device_event(frames=[{"rms": 0.02, "speech_prob": 1.5}], speech_prob=-3.0)
        result = worker_for(_Bridge(event=event)).analyze(describe_window("w-badprob"))
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result.get("speech_prob"))

    def test_a_busy_microphone_names_the_owner(self):
        bridge = _Bridge(mic_owner="speech", listen_mode="speech")
        result = worker_for(bridge).analyze(describe_window("w-busy"))
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("recognizer", result["reason"])
        self.assertEqual(worker_for(bridge).compute_caps(), {})

    def test_capabilities_come_from_the_device_not_from_optimism(self):
        worker = worker_for(_Bridge())
        self.assertEqual(worker.listen_mode, "sound")
        caps = worker.compute_caps()
        self.assertEqual(caps["workloads"], [SOUND_WORKLOAD])
        self.assertEqual(caps["mic_state"], "available")
        self.assertEqual(caps["execution_provider"], "CPU")
        self.assertEqual(caps["models"], {"vad": True, "classifier": True})
        self.assertFalse(caps.get("nnapi", False))
        self.assertNotIn("neural_acceleration", caps)

    def test_mode_off_advertises_nothing_and_refuses(self):
        worker = worker_for(_Bridge(listen_mode="off"))
        self.assertEqual(worker.listen_mode, "off")
        self.assertEqual(worker.compute_caps(), {})
        result = worker.analyze(describe_window("w-off"))
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("off", result["reason"])

    def test_no_runtime_means_no_worker(self):
        self.assertIsNone(android_native_worker(None))
        self.assertIsNone(android_native_worker(_Bridge(available=False)))
        # A device whose mode Python cannot read is not describable, so it gets no worker.
        self.assertIsNone(android_native_worker(_Bridge(listen_mode="broadcast")))

    def test_a_hostile_backend_cannot_make_the_worker_raise(self):
        """The worker documents "never raises". A bad frame used to break that promise by
        failing result validation *after* the work, which took the caller's ingest path down
        instead of answering. Impossible values are now dropped at the frame boundary."""
        class _Hostile:
            def available(self):
                return True

            def mic_state(self):
                return "available"

            def analyze(self, descriptor):
                return {"frames": [{"rms": "loud", "speech_prob": 1.5, "ts_ms": None},
                                   "not-a-dict", None],
                        "labels": [{"name": "", "confidence": 9.0}, "junk",
                                   {"confidence": 0.5}]}

        result = SoundPerceptionWorker(_Hostile()).analyze(describe_window("w-hostile"))
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result.get("speech_prob"))
        self.assertFalse(result.get("labels"))

    def test_an_explicit_bad_mode_is_a_loud_configuration_error(self):
        with self.assertRaises(ValueError):
            android_native_worker(_Bridge(), listen_mode="broadcast")


if __name__ == "__main__":
    unittest.main()