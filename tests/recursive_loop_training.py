#!/usr/bin/env python3
"""
Recursive loop training for ShugoCore on Android devices
=========================================================

Runs repeated full verification rounds against the live runtime + OS:

    force-stop -> OS cold-start -> version check -> 14-phase harness ->
    liveness check -> crash/ANR scan -> (retry on failure) -> next round

Aggregation: per-phase pass rates across rounds, flaky-phase detection, and
a final STABLE / UNSTABLE verdict. No device auto-discovery: pass serials
explicitly via repeated --device arguments.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SMOKE = HERE / "android_device_smoke.py"
PACKAGE = "com.samurai.shugocore"
ADB = "adb"

ALL_PHASES = [
    "service_alive",
    "timer_set",
    "timer_fires_while_away",
    "fact_stores",
    "memory_question",
    "fact_survives_restart",
    "full_teardown_announced",
    "nrr_native_ready",
    "nrr_camera_render",
    "personality_model_genesis",
    "personality_growth_log",
    # v1.30.5: the closed-conversation-loop phases (routing must not depend on
    # a question's phrasing, measurements are never invented, and the agent's
    # own questions get answered).
    "time_tool_query",
    "measurement_honesty",
    "ask_user_round_trip",
]


def sh(args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "(timeout)"


def adb(serial, *args, timeout=30):
    return sh([ADB, "-s", serial, *args], timeout=timeout)


def repo_version():
    src = (ROOT / "version.py").read_text()
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)', src)
    name = m.group(1) if m else "unknown"
    gradle = (ROOT / "platforms/android/app/build.gradle").read_text()
    c = re.search(r"versionCode\s+(\d+)", gradle)
    code = c.group(1) if c else "0"
    return name, code


def device_version(serial):
    rc, out = adb(serial, "shell", "dumpsys", "package", PACKAGE, timeout=40)
    name = code = None
    m = re.search(r"versionName=([\w.]+)", out)
    if m:
        name = m.group(1)
    m = re.search(r"versionCode=(\d+)", out)
    if m:
        code = m.group(1)
    return name, code


def force_stop(serial):
    adb(serial, "shell", "am", "force-stop", PACKAGE, timeout=30)


def pid_of(serial):
    rc, out = adb(serial, "shell", "pidof", PACKAGE, timeout=20)
    parts = out.strip().split()
    return parts[0] if parts else None


def logcat_clear(serial):
    adb(serial, "logcat", "-c", timeout=30)


def crash_scan(serial):
    rc, out = adb(serial, "logcat", "-d", "-t", "2000", timeout=40)
    hits = []
    for pat, label in (
        (r"FATAL EXCEPTION", "fatal_exception"),
        (r"ANR in %s" % re.escape(PACKAGE), "anr"),
        (r"Process %s .* has died" % re.escape(PACKAGE), "process_died"),
    ):
        if re.search(pat, out):
            hits.append(label)
    return hits


# Sanity cap for the smoke-harness subprocess. The fail-fast device check
# in train_device already rejects an unreachable serial, so this only
# bounds a reachable-but-slow run: the full 14-phase set with a measured
# ~50s decision cadence needs up to ~15+ min (personality_growth_log alone
# is ~430s); 1800s leaves margin without resurrecting the old 2h sit.
HARNESS_TIMEOUT = 1800


def extract_harness_json(out):
    """Parse the harness's trailing JSON report from its stdout+stderr.

    The harness prints `json.dumps(payload, indent=2)`, i.e. a MULTI-LINE
    object.  A per-line `startswith("{") and endswith("}")` match therefore
    never fires (interior lines are indented; the final line is a bare
    "}"), so the payload was always None — every round reported
    `results: []`/`phase_stats: {}` and per-phase failures were swallowed.
    Instead, find the last column-0 "{" (the report opens at the top level)
    and decode the remainder with raw_decode, ignoring any trailing output.
    """
    lines = out.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip() == "{":
            blob = "\n".join(lines[idx:])
            try:
                payload, _ = json.JSONDecoder().raw_decode(blob)
            except Exception:
                continue
            if isinstance(payload, dict) and "results" in payload:
                return payload
    return None


def run_harness(serial, phases):
    args = [sys.executable, str(SMOKE), "--device", serial]
    if phases:
        args += ["--phases", *phases]
    args.append("--json")
    rc, out = sh(args, timeout=HARNESS_TIMEOUT)
    payload = extract_harness_json(out)
    if payload is not None:
        # The report is the source of truth for pass/fail and per-phase
        # diagnostics; the harness exits 1 when any phase failed.
        ok = rc == 0 and payload.get("passed") == payload.get("total_phases")
    else:
        ok = rc == 0
    return ok, payload, out


def cold_start_and_verify(serial, expect_name, expect_code, log):
    """Force-stop the app, verify it is down, and confirm the installed
    version matches the repo. The harness's ensure_service_started performs
    the actual OS cold-start inside each round."""
    force_stop(serial)
    time.sleep(3)
    pid = pid_of(serial)
    if pid:
        log.append(f"  WARN process still alive after force-stop (pid {pid})")
        force_stop(serial)
        time.sleep(3)
        pid = pid_of(serial)
        if pid:
            log.append(f"  FAIL could not force-stop (pid {pid})")
            return False
    log.append("  force-stop OK (process down)")
    name, code = device_version(serial)
    if name != expect_name or code != expect_code:
        log.append(f"  FAIL version mismatch: device {name}/{code} "
                   f"!= repo {expect_name}/{expect_code}")
        return False
    log.append(f"  version OK: {name} (versionCode {code})")
    return True


def post_round_health(serial, log):
    """After a round: process liveness + crash/ANR scan of the log buffer."""
    pid = pid_of(serial)
    if not pid:
        log.append("  FAIL no live process after round")
        return False
    log.append(f"  liveness OK (pid {pid})")
    hits = crash_scan(serial)
    if hits:
        log.append(f"  FAIL crash/ANR hits: {', '.join(hits)}")
        return False
    log.append("  crash/ANR scan clean")
    return True


def run_round(serial, ridx, total_rounds, phases, expect_name, expect_code,
              log):
    """One recursive round: cold-stop -> full 14-phase harness -> health."""
    log.append(f"[ROUND {ridx}/{total_rounds}] device {serial}")
    if not cold_start_and_verify(serial, expect_name, expect_code, log):
        return False, []
    logcat_clear(serial)
    ok, payload, harness_out = run_harness(serial, phases)
    results = (payload or {}).get("results", [])
    if payload:
        log.append(f"  harness: {payload.get('passed')}/{payload.get('total_phases')} passed")
    else:
        tail = harness_out.strip().splitlines()[-3:]
        log.append("  harness produced no JSON payload; tail:")
        log.extend("    " + t for t in tail)
    for r in results:
        if not r.get("ok"):
            log.append(f"  FAIL phase {r.get('phase')} (run {r.get('run')})")
    healthy = post_round_health(serial, log)
    return ok and healthy, results


def train_device(serial, rounds, phases, max_retries, cooldown, log):
    """Recursive loop training for one device.

    Each round is a full verification cycle. A failed round is retried up to
    ``max_retries`` times (flakiness tolerance); every attempt is recorded.
    Returns a stability report dict.
    """
    name, code = repo_version()
    log.append(f"repo version: {name} (versionCode {code})")
    dev_name, dev_code = device_version(serial)
    if dev_name is None:
        # device_version returns (None, None) when the ADB query times out
        # or errors -- i.e. the device serial is unreachable. Fail fast
        # instead of letting every round sit on the dead connection.
        log.append(f"FATAL device {serial} is unreachable (ADB query timed "
                   f"out) -- check the connection before training")
        return {"device": serial, "ok": False, "fatal": "device_unreachable",
                "rounds": []}
    if dev_name != name or dev_code != code:
        log.append(f"FATAL device version {dev_name}/{dev_code} does not match "
                   f"repo {name}/{code} -- install the current APK first")
        return {"device": serial, "ok": False, "fatal": "version_mismatch",
                "rounds": []}

    round_records = []
    for ridx in range(1, rounds + 1):
        attempt = 0
        while True:
            attempt += 1
            rlog = []
            ok, results = run_round(serial, ridx, rounds, phases, name, code, rlog)
            log.extend(rlog)
            record = {
                "round": ridx,
                "attempt": attempt,
                "ok": ok,
                "results": results,
            }
            if ok:
                log.append(f"  => ROUND {ridx} PASS (attempt {attempt})")
                round_records.append(record)
                break
            if attempt > max_retries:
                log.append(f"  => ROUND {ridx} FAIL after {attempt} attempts")
                round_records.append(record)
                break
            log.append(f"  retrying round {ridx} (attempt {attempt + 1}/"
                       f"{max_retries + 1}) after cooldown {cooldown}s")
            time.sleep(cooldown)
        if ridx < rounds:
            time.sleep(cooldown)

    # Aggregation across rounds.
    phase_stats = {}
    for rec in round_records:
        for r in rec["results"]:
            st = phase_stats.setdefault(r["phase"], {"pass": 0, "fail": 0})
            st["pass" if r["ok"] else "fail"] += 1
    flaky = [p for p, st in phase_stats.items() if st["fail"] > 0]
    rounds_passed = sum(1 for rec in round_records if rec["ok"])
    total_attempts = sum(rec["attempt"] for rec in round_records)
    stable = (
        rounds_passed == rounds
        and all(rec["attempt"] == 1 for rec in round_records)
    )
    verdict = "STABLE" if stable else ("STABLE_WITH_RETRIES" if rounds_passed == rounds else "UNSTABLE")
    report = {
        "device": serial,
        "repo_version": name,
        "versionCode": code,
        "rounds_requested": rounds,
        "rounds_passed": rounds_passed,
        "total_attempts": total_attempts,
        "phase_stats": phase_stats,
        "flaky_phases": flaky,
        "verdict": verdict,
        "ok": rounds_passed == rounds,
        "rounds": round_records,
    }
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Recursive loop training: repeated cold-start + full "
                    "14-phase verification rounds per device.")
    ap.add_argument("--device", action="append", required=True,
                    help="adb serial (repeat for multiple devices)")
    ap.add_argument("--rounds", type=int, default=3,
                    help="verification rounds per device (default 3)")
    ap.add_argument("--max-retries", type=int, default=2,
                    help="retries per failed round (default 2)")
    ap.add_argument("--cooldown", type=int, default=20,
                    help="seconds between rounds/retries (default 20)")
    ap.add_argument("--phases", nargs="+", default=None,
                    help="phase subset (default: all 9)")
    ap.add_argument("--json", action="store_true",
                    help="emit aggregated JSON report on stdout")
    args = ap.parse_args(argv)

    phases = args.phases if args.phases else list(ALL_PHASES)
    unknown = [p for p in phases if p not in ALL_PHASES]
    if unknown:
        print(f"unknown phases: {', '.join(unknown)}", file=sys.stderr)
        print(f"valid phases: {', '.join(ALL_PHASES)}", file=sys.stderr)
        return 2

    all_ok = True
    reports = []
    for serial in args.device:
        log = []
        print(f"===== TRAINING {serial} =====", flush=True)
        report = train_device(serial, args.rounds, phases,
                              args.max_retries, args.cooldown, log)
        for line in log:
            print(line, flush=True)
        st = report.get("phase_stats", {})
        summary = " ".join(
            f"{p}={v['pass']}/{v['pass'] + v['fail']}" for p, v in st.items())
        print(f"----- {serial}: {report.get('verdict', 'FATAL')} "
              f"({report.get('rounds_passed', 0)}/{args.rounds} rounds, "
              f"{report.get('total_attempts', 0)} attempts) {summary}",
              flush=True)
        reports.append(report)
        all_ok = all_ok and report.get("ok", False)

    if args.json:
        print(json.dumps({"devices": reports, "all_ok": all_ok}, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())