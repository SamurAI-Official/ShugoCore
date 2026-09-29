"""Locating the audio models, without shipping or loading them.

The weights are fetched, never committed (see MODELS.md and
``scripts/fetch_audio_models.py``), so every caller has to cope with their absence:
``find_model`` returns None rather than raising, and callers fail closed.

Search order is explicit so a device build, a host and a test can each point at their
own copy: ``SHUGOCORE_SOUND_MODELS``, the APK asset dir, then ``runtime/audio_models``.
"""
import os
from typing import Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The names are part of the contract with the fetch script and the native layer.
SILERO_VAD = "silero_vad.onnx"
YAMNET = "yamnet.tflite"                 # the reference build, for cross-checking
YAMNET_ONNX = "yamnet_int8.onnx"         # what the app ships (see MODELS.md)
CLASS_MAP = "yamnet_class_map.csv"

SEARCH_DIRS = (
    os.environ.get("SHUGOCORE_SOUND_MODELS") or "",
    os.path.join(REPO_ROOT, "platforms", "android", "app", "src", "main", "assets",
                 "sound"),
    os.path.join(REPO_ROOT, "runtime", "audio_models"),
    os.path.join(REPO_ROOT, "runtime", "models_check"),
)


def search_dirs() -> list:
    return [path for path in SEARCH_DIRS if path]


def find_model(name: str) -> Optional[str]:
    """Absolute path to a model file, or None when it has not been fetched."""
    for directory in search_dirs():
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def have_models() -> bool:
    """True when the models this node would actually run are present.

    Either YAMNet artifact counts: the shipped one is the int8 ONNX, and the TFLite bundle
    is kept as the reference the conversion is verified against.
    """
    return all(find_model(name) for name in (SILERO_VAD, CLASS_MAP)) and any(
        find_model(name) for name in (YAMNET_ONNX, YAMNET))


def load_class_map(path: Optional[str] = None) -> dict:
    """Read the AudioSet index -> display name map, or {} when it is missing."""
    path = path or find_model(CLASS_MAP)
    if not path:
        return {}
    names = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle.read().splitlines()[1:]:
                index, _mid, name = line.split(",", 2)
                names[int(index)] = name.strip().strip('"')
    except (OSError, ValueError):
        return {}
    return names
