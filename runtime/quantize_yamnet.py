import os, numpy as np, onnxruntime as ort
from onnxruntime.quantization import quantize_dynamic, QuantType
M = r'runtime/models_check'
src = os.path.join(M, 'yamnet.onnx')
dst = os.path.join(M, 'yamnet_int8.onnx')
quantize_dynamic(src, dst, weight_type=QuantType.QUInt8)
print('quantized:', os.path.getsize(src), '->', os.path.getsize(dst))

def read_wav(p):
    import wave
    with wave.open(p, 'rb') as h:
        rate = h.getframerate(); raw = h.readframes(h.getnframes())
    d = np.frombuffer(raw, dtype=np.int16).astype(np.float32)/32768.0
    if rate != 16000:
        d = np.interp(np.arange(0, len(d)-1, 16000.0/rate), np.arange(len(d)), d).astype(np.float32)
    return d

names = {}
for line in open(os.path.join(M, 'yamnet_class_map.csv'), encoding='utf-8').read().splitlines()[1:]:
    i, _m, n = line.split(',', 2); names[int(i)] = n.strip().strip(chr(34))

w = np.zeros(15600, dtype=np.float32)
a = read_wav(os.path.join(M, 'speech_16k.wav')); w[:min(len(a),15600)] = a[:15600]
for tag, path in (('fp32', src), ('int8', dst)):
    s = ort.InferenceSession(path, providers=['CPUExecutionProvider'])
    out = s.run(None, {s.get_inputs()[0].name: w})
    sc = np.asarray(out[0]).reshape(-1); emb = np.asarray(out[1]).reshape(-1)
    top = np.argsort(sc)[::-1][:3]
    print(tag, [(names.get(int(i), str(int(i))), round(float(sc[i]),3)) for i in top], 'emb_nonzero', int(np.count_nonzero(emb)))
