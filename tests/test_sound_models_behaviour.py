"""The shipped models' interface and behaviour, as the app's native code assumes them.

sound_jni.cpp hardcodes tensor names and shapes, and SoundProvider asserts in logcat that a
zero window IS silence. Both are checkable here against the exact files the APK ships, so a
model swap or a re-export cannot quietly invalidate either. Skipped when the models have not
been fetched (they are never committed -- see MODELS.md).
"""
from __future__ import annotations

import numpy as np
import pytest

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sound.models import SILERO_VAD, YAMNET_ONNX, find_model  # noqa: E402

ort = pytest.importorskip("onnxruntime", reason="onnxruntime is needed to run the models")
pytestmark = pytest.mark.skipif(
    not (find_model(SILERO_VAD) and find_model(YAMNET_ONNX)),
    reason="audio models not fetched on this host")


@pytest.fixture(scope="module")
def classifier():
    return ort.InferenceSession(find_model(YAMNET_ONNX),
                                providers=["CPUExecutionProvider"])


def test_tensor_names_and_shapes_match_the_native_code(classifier):
    """The C++ sends {"waveform"} and asks for {"activation","global_average_pooling2d"}."""
    assert [i.name for i in classifier.get_inputs()] == ["waveform"]
    assert [i.shape for i in classifier.get_inputs()] == [[15600]]
    outputs = {o.name: o.shape for o in classifier.get_outputs()}
    assert outputs["activation"] == [1, 521]
    assert outputs["global_average_pooling2d"] == [1, 1024]


def test_a_zero_window_is_silence(classifier):
    """The on-device self-test reports this exact fact; if it stops being true, it lies."""
    scores = np.asarray(
        classifier.run(["activation"], {"waveform": np.zeros(15600, dtype=np.float32)})[0]
    ).reshape(-1)
    assert scores.shape == (521,)
    assert int(np.argmax(scores)) == 494, "AudioSet 494 is Silence"


def test_silence_is_distinguishable_from_the_rest(classifier):
    """A label that fires on everything would make the silence claim meaningless."""
    noise = np.random.default_rng(1234).normal(0, 0.25, size=15600).astype(np.float32)
    scores = np.asarray(classifier.run(["activation"], {"waveform": noise})[0]).reshape(-1)
    assert int(np.argmax(scores)) != 494
