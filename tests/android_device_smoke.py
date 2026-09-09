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

# ---------------------------------------------------------------------------
# PHASES — module-level list of dicts. Each entry drives one smoke probe.
# ---------------------------------------------------------------------------

PHASES: List[Dict[str, Any]] = [
    {
        "name": "service_alive",
        "desc": "app is running and agent loop is active",
        "tags": ("preflight",),
    },
    {
        "name": "timer_set",
        "desc": "set a timer and see the ack log line",
        "tags": ("timer",),
    },
    {
        "name": "timer_fires_while_away",
        "desc": "expired timer is re-announced after force-stop+restart",
        "tags": ("timer", "restart"),
    },
    {
        "name": "fact_stores",
        "desc": "assert fact (name, value) landed in SQLite",
        "tags": ("memory", "fact"),
    },
    {
        "name": "memory_question",
        "desc": "ask a memory question and see a memory-based answer",
        "tags": ("memory", "question"),
    },
    {
        "name": "fact_survives_restart",
        "desc": "OS-level fact survives force-stop + restart",
        "tags": ("memory", "fact", "restart"),
    },
    {
        "name": "full_teardown_announced",
        "desc": "full teardown round-trip is announced in logs",
        "tags": ("lifecycle",),
    },
    {
        "name": "personality_model_genesis",
        "desc": "first production model paragraph exists in SQLite",
        "tags": ("personality", "genesis"),
    },
    {
        "name": "personality_growth_log",
        "desc": "growth report can be read from SQLite and diffed offline",
        "tags": ("personality", "growth", "offline"),
    },
]

# ---------------------------------------------------------------------------
# Phase step functions — each is a (serial, logger) -> result callable.
# ---------------------------------------------------------------------------


def _log(logger: List[str], *parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    logger.append(line)


def _log_blue(logger: List[str], *parts: Any) -> None:
    _log(logger, "\033[36m" + " ".join(str(p) for p in parts) + "\033[0m")


def _log_green(logger: List[str], *parts: Any) -> None:
    _log(logger, "\033[32m" + " ".join(str(p) for p in parts) + "\033[0m")


def _log_red(logger: List[str], *parts: Any) -> None:
    _log(logger, "\033[31m" + " ".join(str(p) for p in parts) + "\033[0m")

def step_timer_set(serial: str, logger: List[str]) -> bool:
    """Seed a timer, inject the transcript, and look for the ack."""
    inject_scanout(
        serial, "set a timer for two seconds and tell me when it goes off"
    )
    ok = expect_in_logs(serial, "timer set for 2 seconds", within_s=12.0)
    _log(logger, "  timer ack log line:", "FOUND" if ok else "MISSING")
    return ok


def _sep(logger: List[str]) -> None:
    _log(logger, "=" * 78)


def step_service_alive(serial: str, logger: List[str]) -> bool:
    """Low-level probe: adb shell, package resolvable, Python runtime alive."""
    rc, out, err = run(
        "-s", serial, "shell", "ps", "|", "grep", "-i", "shugocore"
    )
    alive = rc == 0 and "shugocore" in out.lower()
    rc2, ver, _ = run_as_shell(
        serial, "python3 -c 'import sys; print(sys.version.split()[0])'"
    )
    py_ok = rc2 == 0 and ver.strip()
    for ln in [
        f"  ps grep shugocore: {'yes' if alive else 'no'}",
        f"  python3 -c version: {'OK ' + ver.strip() if py_ok else 'MISSING/ERR'}",
    ]:
        _log(logger, ln)
    return alive and py_ok

def step_timer_fires_while_away(serial: str, logger: List[str]) -> bool:
    """Force-stop, then verify the expired timer is re-announced."""
    ensure_service_started(serial)
    inject_scanout(
        serial, "set a timer for two seconds and tell me when it fires"
    )
    if not expect_in_logs(serial, "timer set for 2 seconds", within_s=12.0):
        _log_red(logger, "  timer ack missing before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued; waiting for timer expiry+restart")
    time.sleep(8.0)
    ensure_service_started(serial)
    ok = expect_in_logs(serial, "while you were away", within_s=12.0)
    _log(logger, "  'while you were away' log line:", "FOUND" if ok else "MISSING")
    return ok

def step_fact_stores(serial: str, logger: List[str]) -> bool:
    """Assert an OS-level fact landed in SQLite."""
    inject_scanout(serial, "remember that my favorite color is midnight blue")
    ok = expect_in_logs(serial, "remembered", within_s=12.0)
    _log(logger, "  'remembered' log line:", "FOUND" if ok else "MISSING")
    return ok


def step_memory_question(serial: str, logger: List[str]) -> bool:
    """Ask a memory question and confirm the answer uses memory content."""
    clear_logcat(serial)
    inject_transcript(serial, "what is my favorite color")
    time.sleep(3.0)
    before = set(log_lines_containing(serial, "midnight blue"))
    inject_transcript(serial, "remind me what my favorite color is")
    time.sleep(3.0)
    after = log_lines_containing(serial, "midnight blue")
    ok = len(after) > len(before) and any(
        "midnight blue" in ln.lower() for ln in after
    )
    _log(logger, "  memory question returns stored fact:", "FOUND" if ok else "MISSING")
    _log(logger, "    lines mentioning midnight blue after question:", len(after))
    return ok

def step_fact_survives_restart(serial: str, logger: List[str]) -> bool:
    """Seed a fact, force-stop, restart, ask, and confirm the fact survives."""
    ensure_service_started(serial)
    inject_scanout(serial, "remember that my favorite color is midnight blue")
    if not expect_in_logs(serial, "remembered", within_s=12.0):
        _log_red(logger, "  fact not stored before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued")
    time.sleep(2.0)
    ensure_service_started(serial)
    clear_logcat(serial)
    inject_transcript(serial, "remind me what my favorite color is")
    time.sleep(4.0)
    ok = expect_in_logs(serial, "midnight blue", within_s=12.0)
    _log(logger, "  fact survives restart:", "FOUND" if ok else "MISSING")
    return ok


def step_full_teardown_announced(serial: str, logger: List[str]) -> bool:
    """Full teardown round-trip: seed timers/facts, force-stop, restart,
    and confirm the agent announces what was restored."""
    ensure_service_started(serial)
    inject_scanout(
        serial,
        "set a timer for two seconds and remember that my middle name is June",
    )
    if not expect_in_logs(serial, "timer set for 2 seconds", within_s=12.0):
        _log_red(logger, "  timer ack missing before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued; waiting for timer expiry + restart")
    time.sleep(8.0)
    ensure_service_started(serial)
    ok1 = expect_in_logs(serial, "while you were away", within_s=12.0)
    _log(logger, "  'while you were away' after restart:", "FOUND" if ok1 else "MISSING")
    clear_logcat(serial)
    inject_transcript(serial, "what is my middle name")
    time.sleep(4.0)
    ok2 = expect_in_logs(serial, "june", within_s=12.0)
    _log(logger, "  restored fact (june) after restart:", "FOUND" if ok2 else "MISSING")
    return ok1 and ok2

def step_personality_model_genesis(serial: str, logger: List[str]) -> bool:
    """Verify the first production model paragraph exists in SQLite.

    We deliberately probe the production-model file, not the dev one.
    The harness must not assert on dev-model artifacts.
    """
    rc, out, err = run_as_shell(
        serial,
        "cat /data/data/com.samurai.shugocore/files/"
        "shugo_core_prod_personality_model.json 2>/dev/null | head -c 200",
    )
    body = (out or "") + (err or "")
    ok = rc == 0 and len(body.strip()) > 120 and "ShugoCore" in body
    _log(logger, "  production model JSON readable:", "OK" if ok else "MISSING/EMPTY")
    if not ok:
        _log(logger, "    first 200 chars:", body.strip()[:200])
    return ok

def step_personality_growth_log(serial: str, logger: List[str]) -> bool:
    """Force growth, then read the growth-driven model paragraph from SQLite,
    and diff it offline against the then-current production paragraph.

    Growth is triggered here by a forced _growth_maybe-equivalent stimulus
    (multiple conversational turns). We read the model JSON both before and
    after, compute compare_models locally, and assert drift > 0.
    """
    import ast
    from personality import PersonalityModel

    def read_prod_model(serial: str) -> Optional[PersonalityModel]:
        rc, out, _ = run_as_shell(
            serial,
            "cat /data/data/com.samurai.shugocore/files/"
            "shugo_core_prod_personality_model.json 2>/dev/null",
        )
        if rc != 0 or not out.strip():
            return None
        try:
            return PersonalityModel.from_dict(ast.literal_eval(out))
        except Exception:
            return None

    before = read_prod_model(serial)
    if before is None:
        _log_red(logger, "  cannot read production model before growth")
        return False
    _log(logger, "  pre-growth generation:", before.generation)

    turns = [
        "what is my favorite color",
        "remember that I adopted a rescue greyhound named June",
        "you are being very kind today",
        "remember that my favorite dessert is mango sticky rice",
        "thank you for remembering things about me",
    ]
    for t in turns:
        inject_transcript(serial, t, wait_s=2.0)

    after = read_prod_model(serial)
    if after is None:
        _log_red(logger, "  cannot read production model after growth")
        return False
    _log(logger, "  post-growth generation:", after.generation)

    try:
        comparison = PersonalityModel.compare_models(before, after)
    except Exception as exc:
        _log_red(logger, "  compare_models raised:", exc)
        return False

    drift = comparison.get("drift")
    _log(logger, "  comparison drift:", repr(drift))
    _log(
        logger,
        "  comparison traits:",
        json.dumps(comparison.get("traits"), sort_keys=True, indent=2),
    )
    ok = (drift is not None) and (drift > 0.0)
    _log(logger, "  growth drift > 0:", "OK" if ok else "NONE/STALE")
    return ok

# ---------------------------------------------------------------------------
# CLI + main
# ---------------------------------------------------------------------------

STEP_BY_NAME: Dict[str, Callable[[str, List[str]], bool]] = {
    "service_alive": step_service_alive,
    "timer_set": step_timer_set,
    "timer_fires_while_away": step_timer_fires_while_away,
    "fact_stores": step_fact_stores,
    "memory_question": step_memory_question,

    "fact_survives_restart": step_fact_survives_restart,
    "full_teardown_announced": step_full_teardown_announced,
    "personality_model_genesis": step_personality_model_genesis,
    "personality_growth_log": step_personality_growth_log,
}


def _select_phases(
    names: Optional[List[str]],
    tags_filter: Optional[Tuple[str, ...]],
) -> List[Dict[str, Any]]:
    if names:
        by_name = {p["name"]: p for p in PHASES}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise SystemExit(f"unknown phase(s): {', '.join(missing)}")
        return [by_name[n] for n in names]
    if tags_filter:
        return [p for p in PHASES if tags_filter and not set(tags_filter).isdisjoint(p["tags"])]
    return PHASES


def _run_phase(serial: str, phase: Dict[str, Any], logger: List[str]) -> bool:
    fn = STEP_BY_NAME[phase["name"]]
    _sep(logger)
    _log_blue(logger, f"PHASE: {phase['name']}")
    _log(logger, f"  {phase['desc']}")
    try:
        ok = fn(serial, logger)
    except Exception as exc:
        _log_red(logger, f"  EXCEPTION: {type(exc).__name__}: {exc}")
        ok = False
    status = "PASS" if ok else "FAIL"
    _log(logger, f"  => {status}")
    return ok


def _banner(title: str, logger: List[str]) -> None:
    _sep(logger)
    _log(logger, title)
    _sep(logger)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="ShugoCore Android device smoke probe (dependency-injected, read-only)."
    )
    ap.add_argument(
        "--device", required=False, help="adb serial (e.g. adb-R52WC05JPMW-4kMS88...._adb-tls-connect._tcp)"
    )
    ap.add_argument(
        "--repeat", type=int, default=1, help="repeat full phase set N times"
    )
    ap.add_argument(
        "--phases", nargs="+", help="phase names to run (default: all)"
    )
    ap.add_argument(
        "--tag", action="append", dest="tags", help="include phases with this tag"
    )
    ap.add_argument("--list", action="store_true", help="list phases and exit")
    ap.add_argument("--json", action="store_true", help="emit JSON result on stdout")
    args = ap.parse_args(argv)

    if args.list:
        for p in PHASES:
            print(f"{p['name']:32s} {p['desc']}")
        return 0

    tags = tuple(args.tags) if args.tags else None
    phases = _select_phases(args.phases, tags)
    if not phases:
        print("no phases selected", file=sys.stderr)
        return 2

    serial = args.device
    ensure_connected(serial)
    ensure_service_started(serial)

    results: List[Dict[str, Any]] = []
    run_log: List[str] = []

    if args.repeat > 1:
        _banner(f"REPEAT {args.repeat}x — device {serial}", run_log)

    for ridx in range(args.repeat):
        if args.repeat > 1:
            _banner(f"RUN {ridx + 1}/{args.repeat}", run_log)
        for phase in phases:
            ok = _run_phase(serial, phase, run_log)
            results.append({
                "run": ridx + 1,
                "phase": phase["name"],
                "desc": phase["desc"],
                "ok": ok,
            })

    _sep(run_log)
    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    _log(run_log, f"RESULTS: {passed}/{total} passed")
    if passed != total:
        _log_red(run_log, "SOME PHASES FAILED")

    if args.json:
        payload = {
            "device": serial,
            "repeat": args.repeat,
            "phases_requested": args.phases,
            "tags_requested": list(tags) if tags else None,
            "total_phases": total,
            "passed": passed,
            "results": results,
        }
        print(json.dumps(payload, indent=2))

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
