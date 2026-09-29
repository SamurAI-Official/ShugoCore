#!/usr/bin/env python3
"""Fetch the audio-perception models, pinned by sha256. Never commits them.

Licences live in MODELS.md: Silero VAD is MIT; YAMNet is Apache-2.0 (verified on the Kaggle
model page); the AudioSet class map is CC BY 4.0 and requires attribution. All three land in
the app's asset directory, which is gitignored -- so unverified weights cannot end up in an
MIT repository by accident.

    python3 scripts/fetch_audio_models.py           # fetch what is missing
    python3 scripts/fetch_audio_models.py --check    # verify, download nothing

An unverified download is deleted rather than kept: a model whose hash does not match is
not the model this code was written against, and the contracts in ``sound/schema.py``
(the 576-sample VAD chunk, the 15600-sample YAMNet window) are specific to these files.
"""
import argparse
import hashlib
import os
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Working directory, NOT the app assets: the conversion inputs (yamnet.h5 is 15 MB) and the
# fp32 intermediate would otherwise be packaged into every APK. The three files that ship
# (silero_vad.onnx, yamnet_int8.onnx, yamnet_class_map.csv) are what belongs in
# platforms/android/app/src/main/assets/sound -- see scripts/convert_yamnet_to_onnx.py.
DEFAULT_DIR = os.path.join(REPO_ROOT, "runtime", "audio_models")

MODELS = (
    {
        "name": "silero_vad.onnx",
        "bytes": 2327524,
        "sha256": "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
        "licence": "MIT",
        "url": ("https://raw.githubusercontent.com/snakers4/silero-vad/master/"
                "src/silero_vad/data/silero_vad.onnx"),
    },
    {
        "name": "yamnet.tflite",
        "bytes": 4126810,
        "sha256": "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf",
        "licence": "Apache-2.0 (verified)",
        "url": ("https://storage.googleapis.com/mediapipe-models/audio_classifier/"
                "yamnet/float32/1/yamnet.tflite"),
    },
    {
        # The canonical weights and graph the shipped ONNX is built from. Apache-2.0 allows
        # the derivative; these are fetched so the derivation stays reproducible, and the
        # tflite above stays as the reference the result is checked against.
        "name": "yamnet.h5",
        "bytes": 15296092,
        "sha256": "13c3308955bbfaef262f175ac9c40e47b134573a93984f009220dd7cc12a1744",
        "licence": "Apache-2.0 (canonical weights)",
        "url": "https://storage.googleapis.com/audioset/yamnet.h5",
    },
    {
        "name": "yamnet.py",
        "bytes": 5541,
        "sha256": "7ef3df32b7ecb782490b5a04d7581a23cfcf701dbf476bc4d03deefe22cdb040",
        "licence": "Apache-2.0 (graph definition)",
        "url": ("https://raw.githubusercontent.com/tensorflow/models/master/"
                "research/audioset/yamnet/yamnet.py"),
    },
    {
        "name": "params.py",
        "bytes": 1847,
        "sha256": "925bb1e62461016031f98aea09aeac28975dd516f5747513767de5d1b06b6145",
        "licence": "Apache-2.0 (hyperparameters)",
        "url": ("https://raw.githubusercontent.com/tensorflow/models/master/"
                "research/audioset/yamnet/params.py"),
    },
    {
        "name": "features.py",
        "bytes": 7490,
        "sha256": "e6cd53f81d072c7c43be4c7fff2b9dd0c5ccc7d64f2fbcfc85b44013d6d2ed5e",
        "licence": "Apache-2.0 (log-mel frontend)",
        "url": ("https://raw.githubusercontent.com/tensorflow/models/master/"
                "research/audioset/yamnet/features.py"),
    },
    {
        "name": "yamnet_class_map.csv",
        "bytes": 14096,
        "sha256": "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2",
        "licence": "AudioSet ontology, CC BY 4.0 (attribution)",
        "url": ("https://raw.githubusercontent.com/tensorflow/models/master/"
                "research/audioset/yamnet/yamnet_class_map.csv"),
    },
)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path, model):
    """True when the file on disk is exactly the model this code expects."""
    if not os.path.isfile(path):
        return False
    if os.path.getsize(path) != model["bytes"]:
        return False
    return sha256_file(path) == model["sha256"]


def fetch(model, directory, *, timeout=120):
    """Download one model, verifying before it is kept. Returns a status string."""
    target = os.path.join(directory, model["name"])
    if verify(target, model):
        return "ok (already present)"
    partial = target + ".part"
    try:
        with urllib.request.urlopen(model["url"], timeout=timeout) as response, \
                open(partial, "wb") as handle:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                handle.write(block)
    except Exception as exc:
        if os.path.exists(partial):
            os.remove(partial)
        return "failed: %s" % exc
    if verify(partial, model):
        os.replace(partial, target)
        return "fetched (%d bytes)" % model["bytes"]
    os.remove(partial)
    return "failed: hash or size mismatch (kept nothing)"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch the audio-perception models")
    parser.add_argument("--dir", default=DEFAULT_DIR,
                        help="where to put them (default: the app asset directory)")
    parser.add_argument("--check", action="store_true",
                        help="verify only; download nothing")
    args = parser.parse_args(argv)

    if not args.check:
        os.makedirs(args.dir, exist_ok=True)
    failures = 0
    for model in MODELS:
        target = os.path.join(args.dir, model["name"])
        if args.check:
            status = ("ok" if verify(target, model) else "missing or wrong")
        else:
            status = fetch(model, args.dir)
        if not status.startswith("ok") and not status.startswith("fetched"):
            failures += 1
        print("%-24s %-28s %s" % (model["name"], status, model["licence"]))
    print("\n%s" % ("all models verified" if not failures
                    else "%d model(s) unavailable -- sound perception will report "
                         "not_supported" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
