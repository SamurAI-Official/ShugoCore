"""Scratch: convert the canonical YAMNet (Keras h5) to ONNX with tf2onnx.

The TFLite route needs tflite2onnx, which does not implement the GATHER op inside YAMNet's
graph. Converting the canonical Keras model instead is both the maintained path and a
better artifact: this model exposes the 1024-d embedding next to the 521 scores, which the
MediaPipe bundle does not, so the novelty work (Tier 3) gets its fingerprint for free.

Acceptance is numerical, not structural: on the synthesized speech fixture the TFLite
bundle returns Speech 0.984, and the converted ONNX must return the same top label with a
comparable score. A converter that silently reorders 521 classes would still run, and would
still be wrong.
"""
import json
import os
import sys

os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")   # YAMNet is Keras 2 only
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_check"))

import numpy as np  # noqa: E402

MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_check")
WINDOW = 15600


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


def class_names():
    names = {}
    with open(os.path.join(MODELS, "yamnet_class_map.csv"), encoding="utf-8") as fh:
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
    import tf_keras                                     # noqa: F401  (Keras 2 behind TF)
    import params as params_lib
    import yamnet as yamnet_model

    params = getattr(params_lib, "params", None) or params_lib.Params()
    print("params: classes=%s patch=%sx%s" % (params.num_classes, params.patch_frames,
                                              params.patch_bands))

    model = yamnet_model.yamnet_frames_model(params)
    model.load_weights(os.path.join(MODELS, "yamnet.h5"))
    print("keras outputs:", [tuple(o.shape) for o in model.outputs])

    onnx_path = os.path.join(MODELS, "yamnet.onnx")
    import ml_dtypes
    if not hasattr(ml_dtypes, "float4_e2m1fn"):
        # Environment workaround, not part of the recipe: TF 2.18 pins ml-dtypes<0.5 while
        # onnx 1.22 references a dtype added later. This graph is float32 throughout, so
        # the attribute is never dereferenced; a clean recipe would pin onnx/tf2onnx
        # versions that agree with TF's ml-dtypes pin instead.
        setattr(ml_dtypes, "float4_e2m1fn", np.float16)
    import tf2onnx
    tf2onnx.convert.from_keras(
        model, input_signature=[tf.TensorSpec([WINDOW], tf.float32, name="waveform")],
        opset=13, output_path=onnx_path)
    print("converted:", os.path.getsize(onnx_path), "bytes")

    import onnxruntime as ort
    session = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    inputs = [(i.name, list(i.shape)) for i in session.get_inputs()]
    outputs = [(o.name, list(o.shape)) for o in session.get_outputs()]
    print("onnx inputs :", inputs)
    print("onnx outputs:", outputs)

    window = np.zeros(WINDOW, dtype=np.float32)
    audio = read_wav_16k(os.path.join(MODELS, "speech_16k.wav"))
    window[:min(len(audio), WINDOW)] = audio[:WINDOW]
    names = class_names()

    interp = tf.lite.Interpreter(model_path=os.path.join(MODELS, "yamnet.tflite"))
    interp.allocate_tensors()
    interp.set_tensor(interp.get_input_details()[0]["index"], window.reshape(-1))
    interp.invoke()
    tflite_scores = np.asarray(
        interp.get_tensor(interp.get_output_details()[0]["index"])).reshape(-1)

    shape = inputs[0][1]
    feed_window = window.reshape([1, WINDOW]) if len(shape) == 2 else window.reshape(-1)
    results = session.run(None, {inputs[0][0]: feed_window})
    onnx_scores = np.asarray(results[0]).reshape(-1)
    embedding = np.asarray(results[1]) if len(results) > 1 else None

    print("tflite top5:", top5(tflite_scores, names))
    print("onnx   top5:", top5(onnx_scores, names))
    if embedding is not None:
        print("onnx embedding shape:", embedding.shape, "non-zero:",
              int(np.count_nonzero(embedding)))

    same = int(np.argmax(tflite_scores)) == int(np.argmax(onnx_scores))
    delta = float(np.max(np.abs(tflite_scores - onnx_scores))) \
        if tflite_scores.shape == onnx_scores.shape else float("nan")
    print(json.dumps({"same_top_label": bool(same),
                      "max_score_delta": round(delta, 5),
                      "top": names.get(int(np.argmax(onnx_scores)))}, sort_keys=True))
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
