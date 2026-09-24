#!/usr/bin/env python3
"""Derive the ShugoCore XR Quest3 export preset from a proven reference.

The sample body-tracking-xr-sample ("Meta Quest" preset) already exports
to Quest 3 successfully with this exact Godot build. Copying it wholesale
and applying a documented diff keeps our preset honest: same Gradle build,
same OpenXR mode, same arch — plus internet permission (our bridge is
HTTP) and our package identity.

Usage:
    python3 make_quest_preset.py [reference_preset] [output]

Defaults: the local sample's export_presets.cfg -> platforms/godot/.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_REF = Path("/Users/0exe/body-tracking-xr-sample/export_presets.cfg")
DEFAULT_OUT = REPO / "export_presets.cfg"

# -- ShugoCore deltas over the reference preset -----------------------------
NAME = "Quest3"
UNIQUE_NAME = "com.samurai.shugocore.xr"
EXPORT_PATH = "../build/ShugoCoreXR.apk"


def main() -> int:
    ref = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REF
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    if not ref.exists():
        print(f"reference preset not found: {ref}")
        return 1
    text = ref.read_text()
    # Take ONLY the Meta Quest block (preset.0), not the Android XR block.
    head_end = text.index("[preset.0.options]")
    tail_start = text.index("[preset.1]")
    head = text[:head_end]
    body = text[head_end:tail_start].rstrip() + "\n"

    head = re.sub(r'name="[^"]*"', f'name="{NAME}"', head, count=1)
    head = re.sub(r'export_path="[^"]*"',
                  f'export_path="{EXPORT_PATH}"', head, count=1)
    body = re.sub(r'package/unique_name="[^"]*"',
                  f'package/unique_name="{UNIQUE_NAME}"', body, count=1)
    body = re.sub(r'^permissions/internet=false$',
                  'permissions/internet=true', body, count=1,
                  flags=re.MULTILINE)
    out.write_text(head + body)
    print(f"wrote {out}")
    # Sanity: the flags this project actually depends on.
    for needle in (f'name="{NAME}"', 'xr_features/xr_mode=1',
                   'gradle_build/use_gradle_build=true',
                   'permissions/internet=true',
                   'architectures/arm64-v8a=true'):
        assert needle in out.read_text(), f"MISSING: {needle}"
    print("preset sanity OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
