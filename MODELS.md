# Model licences (what may ship, and what may not)

Every model this project uses is listed here with its licence, because the two
interesting questions about an audio or vision model are *may I redistribute it* and
*does using it make my product non-commercial*. Both answers live in this file so they
are decided once, on the record, instead of being rediscovered.

Weights are **fetched, never committed** (`scripts/fetch_audio_models.py`, hashes
pinned), which keeps NC-licensed material out of an MIT repository by construction.

| Model | Used for | Licence | May ship in the APK? | Where it lives |
|---|---|---|---|---|
| **Silero VAD** v5 (`silero_vad.onnx`, 2.2 MB) | speech / not-speech (Tier 1) | **MIT** — the project states "zero strings attached: no telemetry, no keys, no registration, no built-in expiration" | **Yes** | fetched to `assets/sound/` |
| **YAMNet** (`yamnet.tflite`, 3.9 MB) | 521 AudioSet sound classes (Tier 2) | **Apache-2.0** per Google's model page — *confirm the licence line at source before shipping this in a product* | Yes, once confirmed | fetched to `assets/sound/` |
| **AudioSet class map** (`yamnet_class_map.csv`, 14 KB) | label names for the 521 outputs | AudioSet ontology: **CC BY 4.0** (attribution required) | Yes, **with attribution** | fetched to `assets/sound/` |

## Considered and rejected

| Model | Licence | Why not |
|---|---|---|
| **BirdNET v2.4** (`tflite_int8`, 45.9 MB) | **CC BY-NC** (models) / MIT (code) | The models are **NonCommercial** (Zenodo record: CC BY-NC 4.0; the repo README says CC BY-NC-SA 4.0 for models), and any *derived* artifact — an ONNX conversion, a quantized variant, a fine-tune — inherits it. The code being MIT does not help: the model is the useful part. It is also bird-specific (6,000+ species), 48 kHz where our stream is 16 kHz, and batch tooling rather than a mobile runtime. |
| **PANNs** (`Cnn14_16k` etc.) | **CC BY 4.0** (Zenodo: re-distribution allowed *with attribution*) | Commercially usable, and kept as the *host-side* upgrade path — `Cnn14_16k` has the best mAP (0.438) but is 359 MB, so it belongs on a paired node, not a phone. Note the smaller MobileNetV1/V2 variants want 32 kHz; only `Cnn14_16k` matches our rate. |

## Attribution

If YAMNet and the AudioSet class map ship, the distribution must credit Google and the
AudioSet authors, and note the CC BY 4.0 ontology. Add that to the release NOTICE or the
app's about screen — it is a licence term, not a courtesy.
