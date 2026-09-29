"""The audio-perception contract: what is described, what may be published, and what
happens when there is no model at all.

Three things are worth testing here and one is worth skipping: the Tier 0 arithmetic (no
model, so it always runs), the descriptor/result validation and the bus budget, the
worker's fail-closed behaviour, and -- when the fetched models are present -- the two
real contracts (Silero's 576-sample chunk with a 64-sample context, YAMNet's flat 15600
samples in and 521 scores out). The model tests skip rather than fail without the
weights, because the weights are fetched and never committed.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sound import models  # noqa: E402
from sound.adapter import (SOUND_WORKLOAD, SoundPerceptionWorker,  # noqa: E402
                           observation_payload)
from sound.descriptors import FrameStat, summarize, top_labels  # noqa: E402
from sound.descriptor import SoundWindowDescriptor, describe_window  # noqa: E402
from sound.result import SoundEventResult, not_supported  # noqa: E402
from sound.schema import (SAMPLE_RATE, VAD_CONTEXT, VAD_INPUT,  # noqa: E402
                          VAD_THRESHOLD, WINDOW_SAMPLES, YAMNET_CLASSES)


def frames_of(levels, speech=None):
    """Frames from a list of RMS amplitudes, optionally with VAD probabilities."""
    out = []
    for index, rms in enumerate(levels):
        prob = None if speech is None else speech[index]
        out.append(FrameStat(rms=rms, ts_ms=index * 20.0, speech_prob=prob))
    return out


class Tier0DescriptionTestCase(unittest.TestCase):
    """Acoustic description without a model: pure arithmetic, always available."""

    def test_silence_reads_as_quiet_and_low(self):
        summary = summarize(frames_of([0.0] * 50))
        self.assertEqual(summary.level, "quiet")
        self.assertLess(summary.rms_dbfs, -55.0)
        self.assertEqual(summary.activity_ratio, 0.0)
        self.assertAlmostEqual(summary.longest_silence_ms, 1000.0, delta=1.0)

    def test_a_conversation_and_a_loud_room_are_told_apart(self):
        # Stated in dBFS rather than hand-picked rms, so re-tuning the bands cannot silently
        # turn this into a test of a magic number: -28 is speech across a room, -15 is a shout
        # or a slammed door.
        conversation = [10 ** (-28 / 20.0)] * 20
        shout = [10 ** (-15 / 20.0)] * 20
        self.assertEqual(summarize(frames_of(conversation)).level, "conversational")
        self.assertEqual(summarize(frames_of(shout)).level, "loud")

    def test_mean_energy_does_not_overstate_a_mostly_quiet_window(self):
        """Averaging dBFS would call one loud frame out of twenty 'loud'."""
        quiet_then_loud = summarize(frames_of([0.0] * 19 + [0.3]))
        self.assertEqual(quiet_then_loud.level, "quiet")
        self.assertGreater(quiet_then_loud.peak_dbfs, -20.0)

    def test_an_onset_is_a_jump_not_a_level(self):
        steady = summarize(frames_of([0.05] * 20))
        self.assertEqual(steady.onsets, 0)
        struck = summarize(frames_of([0.0, 0.0, 0.08, 0.08, 0.08]))
        self.assertEqual(struck.onsets, 1)

    def test_trend_needs_a_window_and_a_real_change(self):
        self.assertEqual(summarize(frames_of([0.01, 0.01, 0.01])).trend, "steady")
        rising = summarize(frames_of([0.002, 0.002, 0.004, 0.01, 0.02, 0.03]))
        self.assertEqual(rising.trend, "rising")
        falling = summarize(frames_of([0.03, 0.02, 0.01, 0.004, 0.002, 0.002]))
        self.assertEqual(falling.trend, "falling")

    def test_speech_ratio_comes_from_the_vad_when_it_is_present(self):
        summary = summarize(frames_of([0.02] * 10, speech=[0.9] * 6 + [0.1] * 4))
        self.assertAlmostEqual(summary.speech_ratio, 0.6, delta=0.01)
        self.assertEqual(summarize(frames_of([0.02] * 10)).speech_ratio, 0.0)

    def test_an_empty_window_summarizes_without_raising(self):
        summary = summarize([])
        self.assertEqual(summary.frames, 0)
        self.assertEqual(summary.level, "quiet")
        self.assertEqual(summary.to_dict()["frames"], 0)

    def test_describe_is_one_honest_sentence(self):
        summary = summarize(frames_of([0.02] * 10),
                            labels=[{"name": "Speech", "confidence": 0.98,
                                     "source": "yamnet"}])
        text = summary.describe()
        self.assertIn("sound:", text)
        self.assertIn("Speech 0.98", text)


class PublishingRuleTestCase(unittest.TestCase):
    """A classifier's guess is never published as a fact."""

    def test_labels_below_the_floor_are_dropped_and_the_rest_are_ordered(self):
        labels = top_labels([0.9, 0.05, 0.3], {0: "Speech", 1: "Rattle", 2: "Music"})
        self.assertEqual([label["name"] for label in labels], ["Speech", "Music"])
        self.assertTrue(all(label["source"] == "yamnet" for label in labels))

    def test_labels_are_bounded(self):
        names = {index: "class%d" % index for index in range(40)}
        self.assertEqual(len(top_labels([0.5] * 40, names, limit=5)), 5)

    def test_the_list_form_of_names_is_accepted_too(self):
        labels = top_labels([0.5, 0.4], ["Speech", "Music"])
        self.assertEqual([label["name"] for label in labels], ["Speech", "Music"])

    def test_the_model_contracts_are_pinned_as_verified(self):
        """These numbers came off the real weights; a silent change is a bug."""
        self.assertEqual(VAD_INPUT, 576)          # 512 chunk + 64 context
        self.assertEqual(VAD_CONTEXT, 64)
        self.assertEqual(VAD_THRESHOLD, 0.5)
        self.assertEqual(WINDOW_SAMPLES, 15600)   # 0.975 s at 16 kHz
        self.assertEqual(YAMNET_CLASSES, 521)
        self.assertEqual(SAMPLE_RATE, 16000)


class ContractTestCase(unittest.TestCase):
    def test_a_window_descriptor_round_trips_and_refuses_a_foreign_rate(self):
        descriptor = describe_window("win-1", max_labels=3)
        self.assertEqual(SoundWindowDescriptor.from_dict(descriptor).window_id, "win-1")
        for bad in ({"window_id": "w", "sample_rate": 48000},
                    {"window_id": "w", "samples": WINDOW_SAMPLES - 1},
                    {"window_id": ""},
                    {"window_id": "w", "policy": "broadcast"},
                    {"window_id": "w", "max_labels": 99}):
            with self.assertRaises(ValueError):
                SoundWindowDescriptor.from_dict(bad)

    def test_a_non_ok_result_must_say_why(self):
        with self.assertRaises(ValueError):
            SoundEventResult(window_id="w", status="not_supported").validate()
        result = not_supported("w", "no models fetched")
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("no models fetched", result["reason"])

    def test_a_result_refuses_too_many_labels_or_a_bad_confidence(self):
        with self.assertRaises(ValueError):
            SoundEventResult(window_id="w",
                             labels=[{"name": "x", "confidence": 0.5}] * 6).validate()
        with self.assertRaises(ValueError):
            SoundEventResult(window_id="w",
                             labels=[{"name": "x", "confidence": 2.0}]).validate()


class _FakeBackend:
    """A backend that answers like the device will once the bridge exists."""

    def __init__(self, frames=None, labels=None, caps=None, available=True,
                 raises=False):
        self.frames = frames or []
        self.labels = labels or []
        self.caps = caps or {}
        self._available = available
        self._raises = raises

    def available(self):
        return self._available

    def capabilities(self):
        return self.caps

    def analyze(self, descriptor):
        if self._raises:
            raise RuntimeError("model exploded")
        return {"frames": self.frames, "labels": self.labels, "frame_ms": 20.0}


class WorkerTestCase(unittest.TestCase):
    """Fail-closed: no backend means it says so, not that it heard nothing."""

    def test_without_a_backend_the_worker_advertises_nothing(self):
        worker = SoundPerceptionWorker(None)
        self.assertFalse(worker.available)
        self.assertEqual(worker.compute_caps(), {})
        result = worker.analyze({"window_id": "w"})
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("no audio backend", result["reason"])

    def test_the_stub_keeps_the_same_fail_closed_answer(self):
        answer = SoundPerceptionWorker.worker_stub()({"window_id": "w"})
        self.assertEqual(answer["status"], "not_supported")

    def test_a_backed_worker_summarises_and_bounds_what_it_publishes(self):
        backend = _FakeBackend(
            # -28 dBFS: speech across a room, under the bands re-tuned from device captures.
            frames=[{"rms": 0.04, "speech_prob": 0.9}] * 20,
            labels=[{"name": "Speech", "confidence": 0.98},
                    {"name": "Music", "confidence": 0.30},
                    {"name": "Rattle", "confidence": 0.02}],
            caps={"execution_provider": "CPU", "labels": 521, "speech": True})
        worker = SoundPerceptionWorker(backend)
        self.assertTrue(worker.available)
        result = worker.analyze({"window_id": "w"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual([label["name"] for label in result["labels"]],
                         ["Speech", "Music"])          # 0.02 is below the floor
        self.assertEqual(result["summary"]["level"], "conversational")
        caps = worker.compute_caps()
        self.assertEqual(caps["workloads"], [SOUND_WORKLOAD])
        self.assertEqual(caps["execution_provider"], "CPU")
        self.assertEqual(caps["sample_rate"], 16000)

    def test_caps_stay_empty_when_the_backend_is_unavailable(self):
        worker = SoundPerceptionWorker(_FakeBackend(available=False))
        self.assertFalse(worker.available)
        self.assertEqual(worker.compute_caps(), {})
        self.assertEqual(worker.analyze({"window_id": "w"})["status"], "not_supported")

    def test_a_broken_backend_or_descriptor_is_reported_not_raised(self):
        broken = SoundPerceptionWorker(_FakeBackend(raises=True))
        self.assertIn("backend error", broken.analyze({"window_id": "w"})["reason"])
        worker = SoundPerceptionWorker(_FakeBackend())
        invalid = worker.analyze({"window_id": "w", "sample_rate": 44100})
        self.assertIn("invalid descriptor", invalid["reason"])

    def test_the_worker_audits_what_it_did(self):
        events = []
        worker = SoundPerceptionWorker(
            _FakeBackend(frames=[{"rms": 0.02}] * 5),
            audit=lambda event, payload: events.append((event, payload)))
        worker.analyze({"window_id": "w"})
        self.assertEqual(events[-1][0], "sound_analyzed")
        self.assertEqual(events[-1][1]["window_id"], "w")


class ObservationBudgetTestCase(unittest.TestCase):
    """What travels into the interaction bus has to fit the bus's budget."""

    def _payload(self):
        worker = SoundPerceptionWorker(_FakeBackend(
            frames=[{"rms": 0.02, "speech_prob": 0.9}] * 20,
            labels=[{"name": "Speech", "confidence": 0.98}]))
        return observation_payload(worker.analyze({"window_id": "w"}))

    def test_the_payload_fits_twelve_keys_of_a_hundred_and_sixty_chars(self):
        payload = self._payload()
        self.assertLessEqual(len(payload), 12)
        for key, value in payload.items():
            self.assertLessEqual(len(str(key)), 40)
            self.assertLessEqual(len(str(value)), 160)

    def test_the_payload_is_a_valid_human_observation_of_type_sound(self):
        from human_interaction import HumanObservation

        observation = HumanObservation(type="sound", source="audio_provider",
                                      payload=self._payload(), confidence=0.8)
        ok, reason = observation.validate()
        self.assertTrue(ok, reason)
        self.assertEqual(observation.to_dict()["type"], "sound")
        self.assertIn("level", observation.to_dict()["payload"])


class RealModelContractTestCase(unittest.TestCase):
    """The contracts, against the real weights -- skipped when they are not fetched."""

    @unittest.skipUnless(models.find_model(models.SILERO_VAD),
                         "silero_vad.onnx not fetched (scripts/fetch_audio_models.py)")
    def test_silero_reads_silence_as_not_speech(self):
        try:
            import numpy as np
            import onnxruntime as ort
        except Exception as exc:                       # pragma: no cover - env
            self.skipTest("onnxruntime/numpy unavailable: %s" % exc)
        from sound.schema import VAD_STATE_SHAPE

        session = ort.InferenceSession(models.find_model(models.SILERO_VAD),
                                       providers=["CPUExecutionProvider"])
        self.assertEqual({item.name for item in session.get_inputs()},
                         {"input", "state", "sr"})
        state = np.zeros(VAD_STATE_SHAPE, dtype=np.float32)
        context = np.zeros((1, VAD_CONTEXT), dtype=np.float32)
        loudest = 0.0
        for _ in range(10):
            chunk = np.zeros((1, 512), dtype=np.float32)
            out = session.run(None, {
                "input": np.concatenate([context, chunk], axis=1),
                "state": state, "sr": np.array(16000, dtype=np.int64)})
            state, context = out[1], chunk[:, -VAD_CONTEXT:]
            loudest = max(loudest, float(np.asarray(out[0]).reshape(-1)[0]))
        self.assertLess(loudest, 0.1, "silence must not look like speech")
        self.assertEqual(state.shape, VAD_STATE_SHAPE)

    @unittest.skipUnless(models.find_model(models.YAMNET),
                         "yamnet.tflite not fetched (scripts/fetch_audio_models.py)")
    def test_yamnet_takes_a_flat_window_and_returns_the_class_count(self):
        try:
            import numpy as np
            import tensorflow as tf
        except Exception as exc:                       # pragma: no cover - env
            self.skipTest("tensorflow/numpy unavailable: %s" % exc)

        interp = tf.lite.Interpreter(model_path=models.find_model(models.YAMNET))
        interp.allocate_tensors()
        detail = interp.get_input_details()[0]
        self.assertEqual(list(detail["shape"]), [WINDOW_SAMPLES],
                         "the window contract changed")
        interp.set_tensor(detail["index"],
                          np.zeros(WINDOW_SAMPLES, dtype=np.float32))
        interp.invoke()
        scores = np.asarray(interp.get_tensor(interp.get_output_details()[0]["index"]))
        self.assertEqual(scores.size, YAMNET_CLASSES)

        labels = models.load_class_map()
        self.assertEqual(len(labels), YAMNET_CLASSES)
        self.assertEqual(labels.get(0), "Speech")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_PATH = os.path.join(ROOT, "MODELS.md")
FETCH_SCRIPT = os.path.join(ROOT, "scripts", "fetch_audio_models.py")


def read_file(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def plain(cell):
    """A table cell without markdown emphasis, lower-cased (for comparisons)."""
    return cell.replace("*", "").replace("_", "").strip().lower()


def table_rows(heading=None, stop=None):
    """The `| **name** | ...` rows of a MODELS.md table (optionally one section)."""
    text = read_file(MODELS_PATH)
    if heading:
        text = text.split(heading, 1)[1]
    if stop:
        text = text.split(stop, 1)[0]
    return [[cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in text.splitlines() if line.strip().startswith("| **")]


class LicenceManifestTestCase(unittest.TestCase):
    """Every model the code fetches has a licence decision on the record.

    The point is not the table formatting: it is that "may I ship this, and on what terms"
    is answered in the repository rather than in someone's memory, and that a new model
    cannot arrive without answering it. This is the licence twin of the packaging guard.
    """

    LICENCE_TOKENS = ("MIT", "Apache-2.0", "CC BY")

    def test_every_shipped_model_names_a_licence_and_a_ship_decision(self):
        shipped = table_rows("Model licences (what may ship, and what may not)",
                             "## Considered and rejected")
        self.assertGreaterEqual(len(shipped), 3, "the shipped-model table shrank")
        names = " ".join(row[0] for row in shipped)
        for expected in ("Silero", "YAMNet", "AudioSet"):
            self.assertIn(expected, names)
        for row in shipped:
            self.assertGreaterEqual(len(row), 4, "row missing columns: %r" % row)
            licence = row[2]
            self.assertTrue(any(token in licence for token in self.LICENCE_TOKENS),
                            "no licence named for %s: %r" % (row[0], licence))
            self.assertNotIn("confirm", plain(licence),
                             "a shipped row still hedges its licence: %r" % licence)
            self.assertTrue(plain(row[3]).startswith("yes"),
                            "ship decision is not a plain yes/no: %r" % row[3])

    def test_every_model_the_fetch_script_downloads_is_in_the_manifest(self):
        fetched = re.findall(r'"name": "([^"]+)"', read_file(FETCH_SCRIPT))
        self.assertTrue(fetched, "no models parsed out of the fetch script")
        manifest = read_file(MODELS_PATH)
        for name in fetched:
            self.assertIn(name, manifest,
                          "%s is downloaded but has no licence row in MODELS.md" % name)

    def test_what_was_rejected_says_why_and_the_reason_is_about_licence(self):
        rejected = table_rows("## Considered and rejected", "## Attribution")
        self.assertTrue(rejected, "the rejected-model table disappeared")
        for row in rejected:
            self.assertGreaterEqual(len(row), 3, "row missing columns: %r" % row)
        reasons = " ".join(row[1] for row in rejected)
        self.assertTrue("NC" in reasons or "NonCommercial" in reasons,
                        "no row records a NonCommercial rejection — that decision should "
                        "stay visible")


class _MicBackend(_FakeBackend):
    """A backend that also reports who holds the microphone."""

    def __init__(self, state, **kwargs):
        super().__init__(**kwargs)
        self._state = state

    def mic_state(self):
        return self._state


class MicOwnershipTestCase(unittest.TestCase):
    """A microphone we do not hold is never reported as a quiet room.

    Android gives audio to one capture at a time, and the device's AudioProvider keeps an
    on-device speech recognizer holding the mic continuously. A classifier that opened a
    second stream would read silence -- so ownership is explicit here, and the worker
    refuses to answer rather than inventing a quiet window.
    """

    def test_a_busy_microphone_names_the_owner_and_is_not_a_quiet_room(self):
        worker = SoundPerceptionWorker(_MicBackend("busy_speech",
                                                  frames=[{"rms": 0.0}] * 5))
        result = worker.analyze({"window_id": "w"})
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("speech recognizer holds the microphone", result["reason"])
        self.assertNotIn("summary", result)          # no fabricated quiet window

    def test_a_denied_microphone_says_permission_not_silence(self):
        worker = SoundPerceptionWorker(_MicBackend("denied"))
        self.assertIn("permission is not granted",
                      worker.analyze({"window_id": "w"})["reason"])

    def test_the_capability_is_withheld_while_another_consumer_has_the_mic(self):
        worker = SoundPerceptionWorker(_MicBackend("busy_speech"))
        self.assertEqual(worker.compute_caps(), {})

    def test_listening_off_withholds_the_capability_and_refuses_to_answer(self):
        worker = SoundPerceptionWorker(_MicBackend("available"), listen_mode="off")
        self.assertEqual(worker.compute_caps(), {})
        self.assertIn("off by configuration",
                      worker.analyze({"window_id": "w"})["reason"])

    def test_holding_the_mic_advertises_the_mode_and_the_state(self):
        worker = SoundPerceptionWorker(_MicBackend("available",
                                                  frames=[{"rms": 0.02}] * 5))
        caps = worker.compute_caps()
        self.assertEqual(caps["listen_mode"], "sound")
        self.assertEqual(caps["mic_state"], "available")

    def test_an_unrecognised_mic_state_is_treated_as_absent(self):
        worker = SoundPerceptionWorker(_MicBackend("someone-elses-mic"))
        self.assertEqual(worker.mic_state, "absent")
        self.assertEqual(worker.analyze({"window_id": "w"})["status"], "not_supported")

    def test_a_backend_that_never_reports_mic_state_still_works(self):
        """Backwards compatible: ownership is reported by backends that know about it."""
        worker = SoundPerceptionWorker(_FakeBackend(frames=[{"rms": 0.02}] * 5))
        self.assertEqual(worker.mic_state, "available")
        self.assertEqual(worker.analyze({"window_id": "w"})["status"], "ok")

    def test_an_unknown_listen_mode_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            SoundPerceptionWorker(_MicBackend("available"), listen_mode="broadcast")


if __name__ == "__main__":
    unittest.main()
