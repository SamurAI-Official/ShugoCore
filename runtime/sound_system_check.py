"""Scratch: run the whole chain with the real models, everything but Android itself.

Real 16 kHz audio -> Silero VAD + YAMNet (the shipped int8 ONNX) -> the exact JSON window
SoundProvider publishes -> AndroidSoundBackend -> Tier 0 summary -> result -> bus payload.

The only thing emulated is Kotlin's serialisation step, and that seam is pinned separately
by TestDeviceJsonContract (the key names cannot drift) plus the provider contract tests.
Everything the answer depends on here is real: the models, the class map, the audio, the
backend, the summariser, the envelope.

Run: python runtime/sound_system_check.py
"""
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from sound.adapter import android_native_worker, observation_payload  # noqa: E402
from sound.descriptor import describe_window  # noqa: E402
from sound.models import SILERO_VAD, YAMNET_ONNX, find_model, load_class_map  # noqa: E402
from test_sound_android_backend import _Bridge  # noqa: E402  (the real fixture shape)

WAV = os.path.join(ROOT, "runtime", "models_check", "speech_16k.wav")


def read_wav(path):
    import wave
    with wave.open(path, "rb") as handle:
        rate = handle.getframerate()
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    audio = data.astype(np.float32) / 32768.0
    if rate != 16000:
        step = rate / 16000.0
        audio = np.interp(np.arange(0, len(audio) - 1, step),
                          np.arange(len(audio)), audio).astype(np.float32)
    return audio


def main():
    import onnxruntime as ort

    if not (find_model(SILERO_VAD) and find_model(YAMNET_ONNX)):
        print("models not fetched; nothing to verify")
        return 1
    audio = read_wav(WAV) if os.path.isfile(WAV) else None
    if audio is None:
        print("no speech wav at %s -- using a synthetic rich tone instead" % WAV)
        t = np.arange(15600, dtype=np.float32) / 16000.0
        audio = (0.3 * np.sin(2 * np.pi * 140 * t) + 0.15 * np.sin(2 * np.pi * 280 * t))
        audio = audio.astype(np.float32)

    vad = ort.InferenceSession(find_model(SILERO_VAD), providers=["CPUExecutionProvider"])
    cls = ort.InferenceSession(find_model(YAMNET_ONNX), providers=["CPUExecutionProvider"])

    # -- the frames the device would collect (32 ms chunks, VAD + rms per frame) ------
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, 64), dtype=np.float32)
    frames, probs = [], []
    for start in range(0, len(audio) - 512, 512):
        chunk = audio[start:start + 512]
        if len(chunk) < 512:
            break
        out = vad.run(None, {"input": np.concatenate([context, chunk.reshape(1, -1)], axis=1),
                             "state": state, "sr": np.array(16000, dtype=np.int64)})
        state = out[1]
        context = chunk.reshape(1, -1)[:, -64:]
        prob = float(np.asarray(out[0]).reshape(-1)[0])
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        probs.append(prob)
        frames.append({"rms": rms, "speech_prob": prob})
    print("audio: %.2f s, %d frames, VAD mean=%.3f max=%.3f"
          % (len(audio) / 16000.0, len(frames), float(np.mean(probs)), float(np.max(probs))))

    # -- the window the device would classify ----------------------------------------
    window = audio[:15600] if len(audio) >= 15600 else np.pad(
        audio, (0, 15600 - len(audio))).astype(np.float32)
    scores = np.asarray(cls.run(["activation"], {"waveform": window.astype(np.float32)})[0]
                        ).reshape(-1)
    order = np.argsort(scores)[::-1][:5]
    labels = [{"index": int(i), "score": float(scores[i])} for i in order]

    names = load_class_map()
    print("yamnet top5: %s" % [(names.get(int(i), "?"), round(float(scores[i]), 3))
                               for i in order])

    # -- exactly the JSON SoundProvider.analyze() publishes (see TestDeviceJsonContract) --
    event = json.dumps({"status": "ok", "classified": True, "frame_ms": 32,
                        "frames": frames, "labels": labels,
                        "speech_prob": float(max(probs)), "window_ms": 975})

    worker = android_native_worker(_Bridge(event=event, age_ms=200.0))
    assert worker is not None, "no worker built"
    result = worker.analyze(describe_window("sys-1"))
    payload = observation_payload(result)

    print("\n-- result --")
    print("status      :", result["status"])
    print("summary     :", json.dumps(result.get("summary", {}), sort_keys=True))
    print("labels      :", [(l["name"], round(l["confidence"], 3))
                            for l in result.get("labels", [])])
    print("speech_prob :", result.get("speech_prob"))
    print("bus payload :", json.dumps(payload, sort_keys=True))
    print("caps        :", json.dumps(worker.compute_caps(), sort_keys=True))

    ok = (result["status"] == "ok"
          and result.get("summary", {}).get("level") in ("quiet", "conversational", "loud")
          and bool(result.get("labels"))
          and all(label["name"] for label in result["labels"])
          and 0.0 <= float(result.get("speech_prob", 0.0)) <= 1.0
          and payload.get("heard"))
    print("\nRESULT:", "the chain produces a named, bounded description of real audio"
          if ok else "CHAIN FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
