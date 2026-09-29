#!/usr/bin/env python3
"""Build the ONNX YAMNet the app ships, from the canonical Keras weights.

Why this exists: Apache-2.0 permits a derived artifact, but it asks that the derivation be
knowable. The TFLite bundle cannot simply be converted (its graph uses a GATHER that
tflite2onnx does not implement), so we convert the canonical Keras model with tf2onnx and
quantize with ONNX Runtime -- the same runtime the app already links, which is the point:
no second inference runtime for a small model.

    python3 scripts/convert_yamnet_to_onnx.py
    python3 scripts/convert_yamnet_to_onnx.py --dir runtime/models_check

Inputs (fetched, never committed -- see MODELS.md and fetch_audio_models.py):
  yamnet.h5      the canonical weights
  yamnet.py      the graph definition (Apache-2.0, TensorFlow Authors)
  params.py      its hyperparameters
  features.py    the log-mel frontend
  yamnet.tflite  kept as the reference the converted model is checked against

Output: yamnet_int8.onnx -- flat [15600] waveform in; 521 scores and the 1024-d embedding
out. Verified numerically rather than structurally: the converted model must agree with the
reference bundle on the top class. A converter that silently reordered 521 classes would
still run, and would still be wrong.

Environment notes, learned the hard way:
* YAMNet is Keras 2 only, so TF_USE_LEGACY_KERAS=1 plus the tf-keras package are needed on
  TF 2.16+.
* TF 2.18 pins ml-dtypes<0.5 while onnx>=1.17 dereferences dtypes added later, so pin
  onnx<1.17. The guarded shim below covers a stray mismatch; this graph is float32 only.
"""
import argparse
import hashlib
import json
import os
import sys

os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(REPO_ROOT, "platforms", "android", "app", "src", "main",
                           "assets", "sound")
WINDOW = 15600
REFERENCE = "yamnet.tflite"
CONVERTED = "yamnet.onnx"
SHIPPED = "yamnet_int8.onnx"
REQUIRED = ("yamnet.h5", "yamnet.py", "params.py", "features.py")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build(directory):
    """Build the Keras model from yamnet.py and load the canonical weights."""
    sys.path.insert(0, directory)
    import tensorflow as tf
    import tf_keras                                     # noqa: F401  (Keras 2 shim)
    import params as params_lib
    import yamnet as yamnet_model

    params = getattr(params_lib, "params", None) or params_lib.Params()
    model = yamnet_model.yamnet_frames_model(params)
    model.load_weights(os.path.join(directory, "yamnet.h5"))
    print("model: classes=%s patch=%sx%s outputs=%s"
          % (params.num_classes, params.patch_frames, params.patch_bands,
             [tuple(o.shape) for o in model.outputs]))

    import ml_dtypes
    if not hasattr(ml_dtypes, "float4_e2m1fn"):         # see the docstring
        setattr(ml_dtypes, "float4_e2m1fn", "float16")
    import tf2onnx

    onnx_path = os.path.join(directory, CONVERTED)
    tf2onnx.convert.from_keras(
        model, input_signature=[tf.TensorSpec([WINDOW], tf.float32, name="waveform")],
        opset=13, output_path=onnx_path)
    print("converted fp32: %d bytes" % os.path.getsize(onnx_path))
    return onnx_path


def quantize(onnx_path, directory):
    """Dynamic int8: 16 MB becomes ~4.9 MB, and the embedding survives."""
    from onnxruntime.quantization import QuantType, quantize_dynamic

    target = os.path.join(directory, SHIPPED)
    quantize_dynamic(onnx_path, target, weight_type=QuantType.QUInt8)
    print("quantized: %d bytes (%s)"
          % (os.path.getsize(target), os.path.basename(target)))
    return target


def verify(directory, model_path):
    """The converted model must agree with the reference bundle on the top class."""
    import numpy as np
    import onnxruntime as ort

    session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    inputs = [(item.name, list(item.shape)) for item in session.get_inputs()]
    outputs = [(item.name, list(item.shape)) for item in session.get_outputs()]
    print("inputs :", inputs)
    print("outputs:", outputs)
    if len(outputs) < 2 or outputs[0][1][-1] != 521 or outputs[1][1][-1] != 1024:
        print("FAIL: expected 521 scores and a 1024-d embedding")
        return 1

    names = {}
    with open(os.path.join(directory, "yamnet_class_map.csv"), encoding="utf-8") as fh:
        for line in fh.read().splitlines()[1:]:
            index, _mid, name = line.split(",", 2)
            names[int(index)] = name.strip().strip('"')

    window = np.zeros(WINDOW, dtype=np.float32)
    audio_path = os.path.join(directory, "speech_16k.wav")
    if os.path.isfile(audio_path):
        import wave

        with wave.open(audio_path, "rb") as handle:
            raw = handle.readframes(handle.getnframes())
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        window[:min(len(data), WINDOW)] = data[:WINDOW]

    shape = inputs[0][1]
    feed = window.reshape([1, WINDOW]) if len(shape) == 2 else window.reshape(-1)
    results = session.run(None, {inputs[0][0]: feed})
    scores = np.asarray(results[0]).reshape(-1)
    embedding = np.asarray(results[1]).reshape(-1)
    top = int(np.argmax(scores))

    agree = True
    reference = os.path.join(directory, REFERENCE)
    if os.path.isfile(reference):
        import tensorflow as tf

        interp = tf.lite.Interpreter(model_path=reference)
        interp.allocate_tensors()
        interp.set_tensor(interp.get_input_details()[0]["index"], window.reshape(-1))
        interp.invoke()
        ref = np.asarray(
            interp.get_tensor(interp.get_output_details()[0]["index"])).reshape(-1)
        agree = int(np.argmax(ref)) == top
        delta = float(np.max(np.abs(ref - scores))) if ref.shape == scores.shape \
            else float("nan")
        print("reference top: %s (%.3f) | converted top: %s (%.3f)"
              % (names.get(int(np.argmax(ref))), float(ref.max()),
                 names.get(top), float(scores.max())))
        print("max score delta: %.4f -- the two builds compute mel features differently, "
              "so calibrate thresholds against the build you ship" % delta)
    else:
        print("no reference bundle present; skipping the cross-check")

    print(json.dumps({"top": names.get(top), "same_top_label": bool(agree),
                      "embedding_nonzero": int(np.count_nonzero(embedding)),
                      "sha256": sha256_file(model_path)}, sort_keys=True))
    return 0 if agree else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the shipped ONNX YAMNet")
    parser.add_argument("--dir", default=DEFAULT_DIR,
                        help="where the inputs are and the outputs go")
    parser.add_argument("--skip-verify", action="store_true",
                        help="build only (the acceptance check is the point)")
    args = parser.parse_args(argv)

    missing = [name for name in REQUIRED
               if not os.path.isfile(os.path.join(args.dir, name))]
    if missing:
        print("missing conversion inputs in %s: %s\n(run scripts/fetch_audio_models.py)"
              % (args.dir, ", ".join(missing)))
        return 1
    onnx_path = build(args.dir)
    shipped = quantize(onnx_path, args.dir)
    if args.skip_verify:
        return 0
    return verify(args.dir, shipped)


if __name__ == "__main__":
    sys.exit(main())
