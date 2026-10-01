"""Scratch probe: are the two claims behind the Kotlin sound self-test true?

The service's self-test asserts, on device and in logcat, that a zero window IS
silence -- i.e. the classifier labels it as such instead of inventing something,
and the VAD reads near zero. That claim is falsifiable here, on the host, with the
exact files shipped in the APK: same ONNX models, same ORT, same tensor names the
C++ hardcodes.

Also checks the interface the native code assumes, which is the part nothing else
covers: input name "waveform", outputs "activation"/"global_average_pooling2d",
VAD input 576 (512 new + 64 carried context) and state [2,1,128]. A name mismatch
would surface on-device only as {"status":"run_failed"} -- silence that looks like
a broken model rather than a wiring bug.

Run: python runtime/sound_claim_probe.py
"""
import sys

import numpy as np
import onnxruntime as ort

VAD = r"platforms/android/app/src/main/assets/sound/silero_vad.onnx"
YAMNET = r"platforms/android/app/src/main/assets/sound/yamnet_int8.onnx"

# YAMNet's own silence classes (AudioSet index 0 = Speech itself is NOT silence;
# 494 = Silence, 494-adjacent: 495 White noise, etc. The model's quiet classes.)
SILENCE_ISH = {494, 495, 496, 497}


def describe(session, label):
    print("[%s] inputs=%s" % (label, [(i.name, i.shape) for i in session.get_inputs()]))
    print("[%s] outputs=%s" % (label, [(o.name, o.shape) for o in session.get_outputs()]))
    return session


def top_scores(scores, k=5):
    order = np.argsort(scores)[::-1][:k]
    return [(int(i), float(scores[i])) for i in order]


def main():
    ok = True
    vad = describe(ort.InferenceSession(VAD), "vad")
    cls = describe(ort.InferenceSession(YAMNET), "yamnet")

    # -- the interface the C++ hardcodes -----------------------------------
    cls_inputs = [i.name for i in cls.get_inputs()]
    cls_outputs = [o.name for o in cls.get_outputs()]
    if cls_inputs != ["waveform"]:
        print("FAIL: classifier input names %s, C++ sends ['waveform']" % cls_inputs)
        ok = False
    missing = {"activation", "global_average_pooling2d"} - set(cls_outputs)
    if missing:
        print("FAIL: C++ asks for outputs %s, model has %s" % (sorted(missing), cls_outputs))
        ok = False
    else:
        print("ok: native tensor names match the model")

    # The C++ sends [15600], 1-D -- not [1, 15600]. Shape matters to ORT even when
    # the values are identical, so feed exactly what the device feeds.
    zeros = np.zeros(15600, dtype=np.float32)
    out = cls.run(None, {"waveform": zeros})[0]
    scores = np.asarray(out).reshape(-1)
    print("zero window: %d scores, top=%s" % (scores.size, top_scores(scores)))
    if scores.size != 521:
        print("FAIL: expected 521 classes, got %d" % scores.size)
        ok = False
    top_index = int(np.argmax(scores))
    if top_index in SILENCE_ISH:
        print("ok: zero window is labelled silence (index %d)" % top_index)
    else:
        print("FAIL: zero window labelled %d (not a silence class) -- the "
              "on-device self-test would be reporting a lie" % top_index)
        ok = False

    # -- ...and the falsifiable half: not everything is silence ------------
    rng = np.random.default_rng(1234)
    noise = rng.normal(0, 0.25, size=15600).astype(np.float32)
    n_samples = np.asarray(cls.run(None, {"waveform": noise})[0]).reshape(-1)
    n_top = int(np.argmax(n_samples))
    print("noise window: top=%s" % (top_scores(n_samples),))
    if n_top in SILENCE_ISH:
        print("FAIL: white noise also reads as silence -- the label is not "
              "discriminating anything")
        ok = False
    else:
        print("ok: noise is distinguishable from silence (index %d)" % n_top)

    # -- VAD: near zero on silence, and it must move on noise --------------
    # The VAD's state shape is [2, None, 128] -- symbolic, so it cannot be copied. The
    # fixed shape the C++ also hardcodes is [2, 1, 128]; nothing may be dropped from it.
    names = [i.name for i in vad.get_inputs()]
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, 64), dtype=np.float32)

    def vad_run(chunk):
        nonlocal state, context
        feed = {names[0]: np.concatenate([context, chunk.reshape(1, -1)], axis=1)}
        if len(names) > 1:
            feed[names[1]] = state
        if len(names) > 2:
            feed[names[2]] = np.array(16000, dtype=np.int64)  # sr
        res = vad.run(None, feed)
        if len(res) > 1:
            state = res[1]
        context = chunk[-64:].reshape(1, -1)
        return float(res[0].reshape(-1)[0])

    p_zero = [vad_run(np.zeros(512, dtype=np.float32)) for _ in range(3)][-1]
    p_noise = [vad_run(noise.reshape(-1)[:512]) for _ in range(3)][-1]
    print("vad on silence=%.4f  on noise=%.4f" % (p_zero, p_noise))
    if p_zero > 0.2:
        print("FAIL: VAD reads %.3f on silence" % p_zero)
        ok = False
    if p_noise <= p_zero:
        print("FAIL: VAD does not react to noise")
        ok = False
    if ok:
        print("ok: VAD separates silence from noise")

    print("\nRESULT:", "all claims hold on the shipped files" if ok else "CLAIMS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
