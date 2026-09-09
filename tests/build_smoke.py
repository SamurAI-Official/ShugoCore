#!/usr/bin/env python3
"""Verify the assembled android_device_smoke.py harness.

NOTE: build_smoke.py no longer reconstructs android_device_smoke.py from
fragments — that approach kept corrupting the file. The assembled harness
is maintained directly, and this script only verifies it:
  * syntax parses (ast.parse)
  * exactly one module-level parser (inside main), no stray module-level
    `ap = argparse.ArgumentParser(` blocks
  * --device is registered inside main()
  * --list works
"""
from pathlib import Path
import ast
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
TARGET = HERE / "android_device_smoke.py"

if not TARGET.exists():
    raise SystemExit(f"{TARGET} missing")

src = TARGET.read_text(encoding="utf-8")
try:
    tree = ast.parse(src)
except SyntaxError as e:
    raise SystemExit(f"SYNTAX ERROR line {e.lineno}: {e.msg}")

lines = src.splitlines()
main_at = next(
    (i for i, ln in enumerate(lines) if ln.lstrip().startswith("def main(")),
    None,
)
if main_at is None:
    raise SystemExit("FATAL: def main( not found")

module_level_parsers = [
    i + 1
    for i, ln in enumerate(lines)
    if ln.startswith("ap = argparse.ArgumentParser(") and i < main_at
]
print("module-level parser blocks before main:", module_level_parsers or "none")

main_blob = "\n".join(lines[main_at:])
if "--device" not in main_blob:
    raise SystemExit("FATAL: --device not registered inside main()")
print("--device registered inside main(): yes")

n_add = len(re.findall(r"^[ \t]*ap\.add_argument\(", src, re.MULTILINE))
print("total ap.add_argument( occurrences:", n_add)

r = subprocess.run(
    [sys.executable, str(TARGET), "--list"],
    capture_output=True,
    text=True,
    timeout=30,
)
print("--- --list exit:", r.returncode, "---")
print((r.stdout or "")[:2000])
if r.returncode != 0:
    print(r.stderr[-2000:], file=sys.stderr)
    raise SystemExit("FATAL: --list failed")

print("verified", TARGET, "size:", len(src), "bytes, syntax OK")

