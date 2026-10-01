"""Scratch: verify the real audio models' I/O and behaviour before porting anything.

Runs against the downloaded models in runtime/models_check/ (gitignored). Prints the
exact tensor shapes/dtypes the native port must produce, and sanity-checks that Silero
separates speech-like energy from noise, and that YAMNet's scores/embeddings come out
in the shape we would publish.
"""
import math
import sys

import numpy as np

VAD = r"runtime/models_check/silero_vad.onnx"
YAMNET = r"runtime/models_check/yamnet.tflite"


def tone(seconds, rate, freq, amp=0.3):
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    return (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def read_wav_16k(path):
    """Read a 16-bit mono WAV as float32 in [-1, 1] (stdlib only)."""
    import wave

    try:
        with wave.open(path, "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                return None
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except Exception:
        return None
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != 16000:
        step = rate / 16000.0
        index = np.arange(0, len(data) - 1, step)
        data = np.interp(index, np.arange(len(data)), data).astype(np.float32)
    return data


def main():
    import onnxruntime as ort

    session = ort.InferenceSession(VAD, providers=["CPUExecutionProvider"])
    print("=== silero vad ===")
    print("inputs :", [(i.name, i.shape, i.type) for i in session.get_inputs()])
    print("outputs:", [(o.name, o.shape, o.type) for o in session.get_outputs()])

    # Silero v5 is a stateful RNN over fixed 512-sample chunks at 16 kHz:
    #   input [1, 512]  state [2, 1, 128]  sr []  ->  output [1, 1]  stateN [2, 1, 128]
    state = np.zeros((2, 1, 128), dtype=np.float32)
    rate = np.array(16000, dtype=np.int64)
    sr_input = [i for i in session.get_inputs() if i.name != "input" and "sr" in i.name.lower()]
    print("context inputs:", [i.name for i in sr_input])

    def prob(chunk):
        nonlocal state
        feed = {"input": chunk.reshape(1, -1).astype(np.float32)}
        names = [i.name for i in session.get_inputs()]
        if "state" in names:
            feed["state"] = state
        if "sr" in names:
            feed["sr"] = rate
        out = session.run(None, feed)
        if len(out) > 1:
            state = out[1]
        return float(np.asarray(out[0]).reshape(-1)[0])

    silence = np.zeros(512, dtype=np.float32)
    noise = (np.random.RandomState(0).randn(512) * 0.02).astype(np.float32)
    voiced = tone(0.032, 16000, 140, 0.35) + tone(0.032, 16000, 280, 0.15)
    print("prob(silence) =", round(prob(silence), 4))
    print("prob(noise)   =", round(prob(noise), 4))
    print("prob(harmonic 140+280Hz, 512 samples) =", round(prob(voiced), 4))

    print()
    print("=== yamnet (tflite) ===")
    try:
        import tensorflow as tf
    except Exception as exc:
        print("tensorflow unavailable:", exc)
        return 1
    interp = tf.lite.Interpreter(model_path=YAMNET)
    interp.allocate_tensors()
    for detail in interp.get_input_details():
        print("in :", detail["name"], detail["shape"], detail["dtype"].__name__)
    for detail in interp.get_output_details():
        print("out:", detail["name"], detail["shape"], detail["dtype"].__name__)

    labels = {}
    try:
        with open(r"runtime/models_check/yamnet_class_map.csv", encoding="utf-8") as fh:
            for line in fh.read().splitlines()[1:]:
                index, _mid, name = line.split(",", 2)
                labels[int(index)] = name.strip().strip('"')
    except OSError:
        print("(no class map: printing indices only)")

    audio = read_wav_16k(r"runtime/models_check/speech_16k.wav")
    if audio is None:
        print("no 16 kHz speech wav available for the discrimination check")
        return 0
    print("speech wav samples:", len(audio), "seconds:", round(len(audio) / 16000.0, 2))
    print("speech wav peak=%.4f rms=%.4f" % (float(np.max(np.abs(audio))),
                                             float(np.sqrt(np.mean(audio ** 2)))))

    def silero_run(signal, label):
        # Silero v5 keeps two pieces of state across chunks: the RNN `state` and a
        # 64-sample `context` prepended to each 512-sample chunk (so the input is
        # 576 samples at 16 kHz). Omitting the context makes the model read junk.
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, 64), dtype=np.float32)
        collected = []
        for start in range(0, len(signal) - 512, 512):
            chunk = signal[start:start + 512].reshape(1, -1).astype(np.float32)
            x = np.concatenate([context, chunk], axis=1)
            out = session.run(None, {"input": x, "state": state,
                                     "sr": np.array(16000, dtype=np.int64)})
            state = out[1]
            context = chunk[:, -64:]
            collected.append(float(np.asarray(out[0]).reshape(-1)[0]))
        if collected:
            print("silero on %s: mean=%.3f max=%.3f" % (
                label, float(np.mean(collected)), float(np.max(collected))))
        return collected

    silero_run(np.zeros(16000, dtype=np.float32), "1s silence")
    silero_run((np.random.RandomState(1).randn(16000) * 0.02).astype(np.float32),
               "1s noise")

    # Silero over the real speech, chunk by chunk, carrying the RNN state.
    silero_run(audio, "real speech (as recorded)")
    # Control: the same audio normalised to full scale, in case SAPI's output is quiet.
    peak = float(np.max(np.abs(audio))) or 1.0
    silero_run((audio / peak * 0.9).astype(np.float32), "real speech (normalised)")

    # YAMNet wants the raw waveform as a flat [15600] tensor (1-D, not [1, 15600]).
    window = np.zeros(15600, dtype=np.float32)
    take = min(len(audio), 15600)
    window[:take] = audio[:take]
    interp.set_tensor(interp.get_input_details()[0]["index"], window.reshape(-1))
    interp.invoke()
    scores = np.asarray(interp.get_tensor(interp.get_output_details()[0]["index"])).reshape(-1)
    top = np.argsort(scores)[::-1][:5]
    print("yamnet top5 on real speech:",
          [(labels.get(int(i), str(int(i))), round(float(scores[i]), 3)) for i in top])
    return 0


if __name__ == "__main__":
    sys.exit(main())
