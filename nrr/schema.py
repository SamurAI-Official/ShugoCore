import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict

SCHEMA_VERSION = 1

# Pixel formats the descriptor contract accepts (subset of the NRR Texture
# formats relevant to camera-sourced frames).
KNOWN_PIXEL_FORMATS = ("RGB8", "RGBA8", "YUV420", "GRAY8")

# Render-quality hints, mirroring NRR performance_hints.
KNOWN_QUALITY_HINTS = ("draft", "balanced", "quality")