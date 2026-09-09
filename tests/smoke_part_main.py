#!/usr/bin/env python3
"""Android device smoke probe for ShugoCore.

Dependency-injected only: NO automatic device discovery, NO background
collection, NO adb-without-you-asked, NO personal-data harvesting.

Usage:
        python3 tests/android_device_smoke.py \\
          --device <adb-serial> [--repeat N] [--phases ...] [--json]

Every probe is ad-hoc, read-only, and disposable; the harness does not
build APKs (use platforms/android/gradlew assembleDebug) and does not
collect, store, or transmit logs, transcripts, audio, sensor readings,
or personality-model snapshots beyond the read-only run-as cat used
for assertion.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ADB = str(Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb")
SHUGOCORE_PACKAGE = "com.samurai.shugocore"

INJECT_ACTION = f"{SHUGOCORE_PACKAGE}.INJECT_TRANSCRIPT"
INJECTION_KEY = "text"

PRODUCTION_MODEL_NAME = "shugocore-ollama"


# ---------------------------------------------------------------------------
# adb utility helpers (all ad-hoc, no background collection)
# ---------------------------------------------------------------------------

def run(*args: str) -> Tuple[int, str, str]:
    proc = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=90)
    return proc.returncode, proc.stdout, proc.stderr


def device_connected(serial: str) -> bool:
    rc, out, _ = run("-s", serial, "get-state")
    return rc == 0 and out.strip() == "device"


def ensure_connected(serial: str) -> None:
    if not device_connected(serial):
        raise SystemExit(f"device not reachable: {serial!r}")


def log_lines_containing(serial: str, needle: str) -> List[str]:
    rc, out, _ = run("-s", serial, "logcat", "-d", "-v", "brief")
    if rc != 0:
        return []
    return [ln for ln in out.splitlines() if needle.lower() in ln.lower()]


def clear_logcat(serial: str) -> None:
    run("-s", serial, "logcat", "-c")


def expect_in_logs(serial: str, needle: str, within_s: float = 15.0) -> bool:
    deadline = time.time() + within_s
    while time.time() < deadline:
        if log_lines_containing(serial, needle):
            return True
        time.sleep(1.0)
    return False


def run_as_shell(serial: str, *shell_args: str) -> Tuple[int, str, str]:
    rc, out, err = run(
        "-s", serial, "shell", "run-as",
        SHUGOCORE_PACKAGE, "sh", "-c", *shell_args,
    )
    return rc, out, err


def inject_transcript(serial: str, text: str, wait_s: float = 1.5) -> None:
    """Send a transcript via the debug broadcast.

    The --es extra is passed as a single arg so multi-word text survives
    adb->am parsing intact (avoids pkg=time mangling 'what time is it').
    """
    run(
        "-s", serial, "shell", "am", "broadcast", "-a", INJECT_ACTION,
        "--es", INJECTION_KEY, text,
    )
    time.sleep(wait_s)


def ensure_service_started(serial: str) -> None:
    """Force the foreground service back up after a force-stop.

    am startservice was rejected by the manifest; the fallback is to
    foreground the app via MainActivity, which (presumably) restarts the
    foreground service. We sleep a few seconds for the Python side to
    re-initialise before injecting more transcripts.
    """
    run(
        "-s", serial, "shell", "am", "start", "-n",
        f"{SHUGOCORE_PACKAGE}/.MainActivity",
    )
    time.sleep(4.0)


def inject_scanout(serial: str, text: str, wait_s: float = 2.0) -> None:
    """Convenience: clear logcat, inject, then wait.

    Callers that want to observe what the agent emitted after an
    injection do:
        clear_logcat(serial)
        inject_scanout(serial, "the transcript")
        assert expect_in_logs(serial, "expected fragment")
    """
    clear_logcat(serial)
    inject_transcript(serial, text)
    time.sleep(wait_s)


def _longest_match(log_lines: List[str], pattern: str) -> Optional[str]:
    pat = re.compile(pattern, re.DOTALL)
    best: Optional[str] = None
    best_len = 0
    for ln in log_lines:
        m = pat.search(ln)
        if m and (m.end() - m.start()) > best_len:
            best = m.group(1).strip()
            best_len = m.end() - m.start()
    return best


def extract_json_from_log(serial: str, label: str) -> Optional[Dict]:
    """Grab a JSON-ish blob that looks like a Python repr or lit.

    Not a full parser — we just pull the largest {...} or [...] span
    in lines that mention *label*, then eval it with ast.literal_eval.
    """
    import ast
    lines = log_lines_containing(serial, label)
    if not lines:
        return None
    blob = _longest_match(
        lines,
        r"\s*(\[(?:[^{}]|(?:\[(?:[^{}]|\[(?:[^{}]|\[[^\]]*\])*\])|(?:\{[^{}]*(?:\{[^{}]*\})*\}))*)\]|\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})",
    )
    if not blob:
        return None
    try:
        return ast.literal_eval(blob)
    except Exception:
        return None
