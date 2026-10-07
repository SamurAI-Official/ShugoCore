#!/usr/bin/env python3
"""Inspect a built APK: its manifest, and the Python it actually bundles.

A development tool, not runtime. It was written as a bare script, so *importing*
it executed all of it -- opening
`platforms/android/app/build/outputs/apk/debug/app-debug.apk` and dying with
`FileNotFoundError` on any machine without that exact build. Since it ships in
the wheel (it is a declared `py-modules` entry), `import check_apk` was a module
that could not be imported. The body lives in `main()` now, and the archive to
inspect is an argument.
"""
import io
import re
import sys
import zipfile
from pathlib import Path

DEFAULT_APK = Path("platforms/android/app/build/outputs/apk/debug/app-debug.apk")

def parsem(text: str):
    # AndroidManifest.xml is binary xml. Try to parse as XML after stripping bogus bytes.
    import html
    text = html.unescape(text)
    # decode binary xml entity-encoded chars: &#...; and &#x...;
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1),16)), text)
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    return text

def _print_safe(text: str) -> None:
    """Print decoded manifest bytes without dying on a cp1252 console.

    The manifest is binary XML, so the decoded text can contain bytes a cp1252
    console cannot encode: printing it raw raises UnicodeEncodeError and the
    check fails before it reports anything at all.
    """
    print(text.encode("ascii", "replace").decode("ascii"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apk_path = Path(argv[0]) if argv else DEFAULT_APK
    if not apk_path.is_file():
        print(f"no APK at {apk_path} -- build one, or pass a path", file=sys.stderr)
        return 2

    with zipfile.ZipFile(apk_path) as apk:
        names = apk.namelist()
        text = parsem(apk.read("AndroidManifest.xml").decode("utf-8", "replace"))
        print("=== parsed manifest (first 1000 chars) ===")
        _print_safe(text[:1000])
        print("=== parsed manifest (last 1000 chars) ===")
        _print_safe(text[-1000:])

        print("\n=== python bundle: governor/personality files ===")
        # Chaquopy ships the app's Python as assets/chaquopy/app.imy, so looking
        # only for a "python/" prefix (the layout from years ago) lists nothing
        # and reports a good APK as empty. Read the archive it is actually in.
        bundle_names = []
        for candidate in ("assets/chaquopy/app.imy", "assets/chaquopy/app.zip"):
            if candidate in names:
                try:
                    with zipfile.ZipFile(io.BytesIO(apk.read(candidate))) as bundle:
                        bundle_names = bundle.namelist()
                    print(f"  ({candidate}: {len(bundle_names)} entries)")
                except Exception as exc:      # noqa: BLE001 - report, keep going
                    print(f"  ({candidate} unreadable: {exc})")
                break
        for n in sorted(bundle_names):
            if ("governor" in n or "personality" in n
                    or "model.py" in n or "growth.py" in n):
                print("  ", n)
        legacy = sum(1 for n in names if n.startswith("python/"))
        print("  legacy python/ entries:", legacy)

        print("\n=== python bundle count ===")
        print("total python files:", len(bundle_names) + legacy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
