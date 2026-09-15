"""Android CSFA soak — endurance on real hardware, watched over adb.

Soaks the on-device CSFA loop for a wall-clock duration and verifies, per
poll, the properties that matter on hardware:

  1. the agent process is alive (pidof),
  2. no native crashes landed in logcat (Fatal signal / SIGABRT / SIGSEGV /
     SIGBUS / FORTIFY),
  3. no new Python tracebacks on the Chaquopy stderr bridge,
  4. the decision loop keeps advancing ("Decision made for task" lines grow;
     two consecutive polls with zero growth is a stall),
  5. optionally, a transcript is injected every --inject-every seconds so the
     conversational fast path stays exercised too.

The Chaquopy runtime is embedded (no python binary to exec), so every signal
is read from logcat — the same convention as android_device_smoke.py. Exit 0
only when every device reports STABLE for the whole run.

Usage::

    python3 tests/android_device_soak.py --minutes 15
    python3 tests/android_device_soak.py --serial adb-XYZ --minutes 60 --json
"""
import argparse
import json
import re
import sys
import time
from typing import Dict, List, Optional

from android_device_smoke import (
    SHUGOCORE_PACKAGE,
    clear_logcat,
    ensure_service_started,
    inject_transcript,
    run,
)

CRASH_RE = re.compile(r"Fatal signal|SIGABRT|SIGSEGV|SIGBUS|FORTIFY")
DECISION_NEEDLE = "Decision made for task"
TRACEBACK_NEEDLE = "Traceback (most recent call last)"
# No decision for this long (with the process alive) means the loop is wedged.
# Real-device cadence is ~30-40 s per decision on a 0.5B baseline and
# conversational ticks produce no decision line at all, so this must exceed
# several slow cycles — it is a wedge detector, not a pace judge.
STALL_AFTER_S = 300.0


def _dump(serial: str) -> str:
    rc, out, _ = run("-s", serial, "logcat", "-d")
    return out if rc == 0 else ""


def _device_poll(serial: str, state: Dict) -> Dict[str, object]:
    """One poll of the soak invariants for one device."""
    rc, out, _ = run("-s", serial, "shell", "pidof", SHUGOCORE_PACKAGE)
    alive = rc == 0 and out.strip() != ""
    if not alive:
        state["violations"].append(
            {"ts": round(time.time(), 3), "kind": "process_dead"})
        return {"alive": False, "violations_new": 1}

    lines = _dump(serial).splitlines()

    crashes = sum(1 for ln in lines if CRASH_RE.search(ln))
    if crashes > state["crashes"]:
        state["violations"].append(
            {"ts": round(time.time(), 3), "kind": "native_crash",
             "detail": [ln.strip()[:200] for ln in lines
                        if CRASH_RE.search(ln)][:3]})
    state["crashes"] = max(state["crashes"], crashes)

    tracebacks = sum(1 for ln in lines if TRACEBACK_NEEDLE in ln)
    if tracebacks > state["tracebacks"]:
        sample = next((ln.strip()[:200] for ln in lines
                       if TRACEBACK_NEEDLE in ln), "")
        state["violations"].append(
            {"ts": round(time.time(), 3), "kind": "python_traceback",
             "detail": sample})
    state["tracebacks"] = max(state["tracebacks"], tracebacks)

    decisions = sum(1 for ln in lines if DECISION_NEEDLE in ln)
    delta = decisions - state["decisions"]
    now = time.monotonic()
    state["decisions"] = decisions
    state["deltas"].append(delta)
    # Stall semantics: the loop is "wedged" only when NO decision lands for
    # STALL_AFTER_S regardless of polls. Real devices have heterogeneous
    # cadence (a 0.5B model decides every ~30-40 s; a conversational
    # fast-path tick — e.g. an injected check-in — logs no decision line and
    # can hold the loop for a full model cycle), so short idle patches are
    # normal. Growth resets the clock; STALL_AFTER_S of silence is a stall.
    if delta > 0:
        state["last_growth"] = now
    elif state["decisions"] > 0 and state["last_growth"] is not None:
        silent_for = now - state["last_growth"]
        if silent_for > STALL_AFTER_S:
            state["violations"].append(
                {"ts": round(time.time(), 3), "kind": "loop_stall",
                 "detail": f"no new decisions for {silent_for:.0f}s "
                           f"(total {decisions})"})
            state["last_growth"] = now  # report once per stall episode

    return {"alive": True, "decision_delta": delta,
            "decisions_total": decisions,
            "crashes": crashes, "tracebacks": tracebacks}


def _soak_device(serial: str, minutes: float, poll: float,
                 inject_every: float) -> Dict:
    clear_logcat(serial)
    try:
        ensure_service_started(serial)
    except Exception:
        pass  # the alive poll reports a dead process and recovery kicks in
    time.sleep(3.0)
    state = {"decisions": 0, "crashes": 0, "tracebacks": 0,
             "violations": [], "polls": 0,
             "injects": 0, "deltas": [], "last_growth": None}
    deadline = time.monotonic() + minutes * 60.0
    next_inject = time.monotonic() + inject_every if inject_every > 0 else None
    while time.monotonic() < deadline:
        last = _device_poll(serial, state)
        state["polls"] += 1
        if not last["alive"]:
            # Try to bring the service back so the soak keeps observing.
            try:
                ensure_service_started(serial)
            except Exception:
                pass
        if next_inject is not None and time.monotonic() >= next_inject:
            inject_transcript(serial, "soak check-in: how are you doing?")
            state["injects"] += 1
            next_inject = time.monotonic() + inject_every
        time.sleep(poll)
    verdict = "STABLE" if not state["violations"] else "VIOLATIONS"
    deltas = [d for d in state["deltas"] if d is not None]
    return {"serial": serial, "verdict": verdict, "minutes": minutes,
            "polls": state["polls"], "injects": state["injects"],
            "decisions_total": state["decisions"],
            "decision_deltas": {"min": min(deltas) if deltas else 0,
                                "max": max(deltas) if deltas else 0,
                                "idle_polls": sum(1 for d in deltas if d <= 0)},
            "native_crashes": state["crashes"],
            "python_tracebacks": state["tracebacks"],
            "violations": state["violations"]}


def _attached_serials() -> List[str]:
    rc, out, _ = run("devices")
    if rc != 0:
        return []
    serials = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="android_device_soak",
        description="Android CSFA soak over adb (see module docstring)")
    parser.add_argument("--serial", action="append", default=[],
                        help="device serial (repeatable; default: all attached)")
    parser.add_argument("--minutes", type=float, default=15.0,
                        help="soak duration per device, in minutes")
    parser.add_argument("--poll", type=float, default=30.0,
                        help="seconds between invariant polls")
    parser.add_argument("--inject-every", type=float, default=300.0,
                        help="seconds between transcript injections "
                             "(0 disables)")
    parser.add_argument("--json", action="store_true",
                        help="print only the JSON report on stdout")
    args = parser.parse_args(argv)

    serials = args.serial or _attached_serials()
    if not serials:
        print("no devices attached", file=sys.stderr)
        return 2

    reports = []
    # Sequential on purpose: the soak is about stability, not throughput, and
    # per-device decision-line accounting stays clean.
    for serial in serials:
        if not args.json:
            print(f"=== soaking {serial} for {args.minutes} min "
                  f"(poll {args.poll}s) ===", flush=True)
        reports.append(_soak_device(serial, args.minutes, args.poll,
                                    args.inject_every))
    report = {"verdict": "STABLE" if all(r["verdict"] == "STABLE"
                                         for r in reports) else "VIOLATIONS",
              "devices": reports}
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "STABLE" else 1


if __name__ == "__main__":
    sys.exit(main())
