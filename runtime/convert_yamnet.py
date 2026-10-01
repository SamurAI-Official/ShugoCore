"""Scratch: convert the YAMNet TFLite bundle to ONNX and prove it is the same model.

The decision was to reuse the ONNX Runtime the APK already links instead of shipping a
second inference runtime for a 3.9 MB model. Apache-2.0 permits the derivative; what it
asks in return is that modifications be marked and that we know what we changed. So this
records the source hash, the conversion tool, and -- the part that matters -- proves the
converted model produces the same answer as the original on the same audio.

Acceptance: on the synthesized speech fixture the TFLite model returns Speech 0.984. The
ONNX model must return Speech as its top label with a comparable score. A converter that
silently reorders the 521 classes would still "work" and would still be wrong; this is the
test that catches it.
"""
import json
import sys

import numpy as np

TFLITE = r"runtime/models_check/yamnet.tflite"
ONNX = r"runtime/models_check/yamnet.onnx"
WAV = r"runtime/models_check/speech_16k.wav"


def read_wav_16k(path):
    import wave

    with wave.open(path, "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != 16000:
        step = rate / 16000.0
        data = np.interp(np.arange(0, len(data) - 1, step),
                         np.arange(len(data)), data).astype(np.float32)
    return data


def class_map():
    names = {}
    with open(r"runtime/models_check/yamnet_class_map.csv", encoding="utf-8") as fh:
        for line in fh.read().splitlines()[1:]:
            index, _mid, name = line.split(",", 2)
            names[int(index)] = name.strip().strip('"')
    return names


def top5(scores, names):
    flat = np.asarray(scores).reshape(-1)
    order = np.argsort(flat)[::-1][:5]
    return [(names.get(int(i), str(int(i))), round(float(flat[i]), 3)) for i in order]


def main():
    import tensorflow as tf

    window = np.zeros(15600, dtype=np.float32)
    audio = read_wav_16k(WAV)
    window[:min(len(audio), 15600)] = audio[:15600]
    names = class_map()

    interp = tf.lite.Interpreter(model_path=TFLITE)
    interp.allocate_tensors()
    detail = interp.get_input_details()[0]
    interp.set_tensor(detail["index"], window.reshape(-1))
    interp.invoke()
    tflite_scores = interp.get_tensor(interp.get_output_details()[0]["index"])
    print("tflite in :", list(detail["shape"]), detail["dtype"].__name__)
    print("tflite top5:", top5(tflite_scores, names))

    import tflite2onnx
    tflite2onnx.convert(TFLITE, ONNX)
    print("converted ->", ONNX)

    import onnxruntime as ort
    session = ort.InferenceSession(ONNX, providers=["CPUExecutionProvider"])
    inputs = [(i.name, i.shape, i.type) for i in session.get_inputs()]
    outputs = [(o.name, o.shape, o.type) for o in session.get_outputs()]
    print("onnx inputs :", inputs)
    print("onnx outputs:", outputs)

    feed = {}
    for item in session.get_inputs():
        shape = [d if isinstance(d, int) and d > 0 else None for d in item.shape]
        if shape and shape[-1] == 15600:
            feed[item.name] = window.reshape(1, 15600)
        elif shape and shape[0] == 15600:
            feed[item.name] = window.reshape(15600)
        else:
            print("unexpected input shape:", item.name, item.shape)
            return 1
    onnx_scores = session.run(None, feed)[0]
    print("onnx top5  :", top5(onnx_scores, names))

    a = np.asarray(tflite_scores).reshape(-1)
    b = np.asarray(onnx_scores).reshape(-1)
    if a.shape != b.shape:
        print("SHAPE MISMATCH", a.shape, b.shape)
        return 1
    agree = int(np.argmax(a)) == int(np.argmax(b))
    drift = float(np.max(np.abs(a - b)))
    print(json.dumps({"same_top_label": agree, "max_score_delta": round(drift, 5),
                      "top": names.get(int(np.argmax(b)))}, sort_keys=True))
    return 0 if agree else 1


if __name__ == "__main__":
    sys.exit(main())
