# Third-party attribution

ShugoCore's own code is MIT (see `LICENSE`). This file records what third-party material
that code ships, so attribution is a maintained fact rather than something reconstructed at
release time. Audio and model licences are decided in **MODELS.md**, which also records what
was considered and rejected.

## Audio perception models

| Component | Licence | Attribution |
|---|---|---|
| **YAMNet** (`yamnet.tflite`) — Google, 2025 | **Apache-2.0** | Google. Include the Apache-2.0 text; the file is redistributed unmodified (verified by sha256, see `scripts/fetch_audio_models.py`). |
| **AudioSet ontology** — the 521 class names in `yamnet_class_map.csv` | **CC BY 4.0** | Gemmeke, Ellis, Freedman, Jansen, Lawrence, Moore, Plakal, Ritter. *"Audio Set: An ontology and human-labeled dataset for audio events"*, ICASSP 2017. |
| **Silero VAD** (`silero_vad.onnx`) — Silero Team | **MIT** | Silero Team, `snakers4/silero-vad`. |

## Native runtimes shipped in the APK

These are the third-party binaries present in a debug `app-debug.apk` (verified by listing
the packaged `lib/arm64-v8a`), listed here so the surface is concrete rather than hand-waved.
Their licence texts still need to be included properly — see the gap below.

| Binary | Component | Licence |
|---|---|---|
| `libllama_jni.so` (contains ggml) | llama.cpp / ggml | MIT |
| `libonnxruntime.so` | ONNX Runtime | MIT |
| `libnrr_jni.so` | upstream NRR (vendored in `cpp/nrr`) + our bridge | MIT |
| `libchaquopy_java.so`, `libpython3.13.so`, `assets/chaquopy/*` | Chaquopy, CPython | MIT, PSF-2.0 |
| `libsqlite3_python.so`, `libsqlite3_chaquopy.so` | SQLite | Public domain |
| `libssl_python.so`, `libcrypto_python.so`, `libssl_chaquopy.so`, `libcrypto_chaquopy.so` | OpenSSL | Apache-2.0 |
| `libc++_shared.so`, `libomp.so` | LLVM libc++ / libomp | Apache-2.0 with LLVM exception |
| `libimage_processing_util_jni.so` | AndroidX CameraX | Apache-2.0 |

## Gap (deliberate, not an oversight)

Until this file existed there was **no attribution surface in the repository at all** —
llama.cpp, ONNX Runtime and Chaquopy were credited only in prose in the README. The licences
above are stated from each project's own terms; what is still missing is the mechanical part:
bundling the actual licence texts (and any upstream `NOTICE` files) so a distributed APK
carries them, plus a test asserting every shipped `.so` maps to a row here. That is a
separate piece of work from the audio feature, and it is recorded here rather than left
implicit.
