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

# The harness lives in tests/ but imports the personality package (repo
# root) for the offline compare_models diff; sys.path[0] is tests/ when
# run as `python3 tests/android_device_smoke.py`, so add the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

INJECT_ACTION = f"{SHUGOCORE_PACKAGE}.INJECT_TRANSCRIPT"
INJECTION_KEY = "text_b64"  # base64 extra key (see inject_transcript)

PRODUCTION_MODEL_NAME = "shugocore-ollama"

# Measured decision cadence (s) for the agent loop, sampled once at start.
# Phase windows scale off this: v1.28.2 restored REAL on-device model
# decisions (delegation fix), so each decision takes tens of seconds; the
# harness must not assume the 1 Hz fallback cadence it was calibrated on.
_CADENCE_S = 1.0


def ack_window() -> float:
    """Window to observe one model-answered transcript ack."""
    return max(12.0, _CADENCE_S * 2.5)


def restart_window() -> float:
    """Window for the agent loop to resume + answer after a restart."""
    return max(30.0, _CADENCE_S * 2.0)


def measure_decision_cadence(serial: str) -> float:
    """Estimate the inter-decision gap (s) of the agent loop.

    Each decision is logged twice (standard logger + logging_manager), so
    line-count polling would measure 0.  We instead track DISTINCT decision
    timestamps and return the wall time between the first and the second
    distinct decision.  Clamped to [1.0, 90.0]; when fewer than two
    decisions appear within 100 s, returns a conservative real-model floor
    of 50 s so phase windows still tolerate slow generations.
    """
    ts_pat = re.compile(r"(\d{2}:\d{2}:\d{2})\.\d{3}")
    clear_logcat(serial)
    start = time.time()
    seen: set = set()
    first_seen_at: Optional[float] = None
    while time.time() - start < 100.0:
        for ln in log_lines_containing(serial, " - Decision made for task"):
            m = ts_pat.search(ln)
            if not m:
                continue
            ts = m.group(1)
            if ts in seen:
                continue
            seen.add(ts)
            if first_seen_at is None:
                first_seen_at = time.time()
            else:
                return min(90.0, max(1.0, time.time() - first_seen_at))
        time.sleep(0.5)
    return 50.0


# ---------------------------------------------------------------------------
# adb utility helpers (all ad-hoc, no background collection)
# ---------------------------------------------------------------------------

def run(*args: str) -> Tuple[int, str, str]:
    # errors="replace": the logcat ring can contain arbitrary device bytes;
    # the harness must never crash decoding them.
    proc = subprocess.run([ADB, *args], capture_output=True, text=True,
                          timeout=90, errors="replace")
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
    """Run a command in the package sandbox as the app user.

    NOTE: `sh -c` under run-as HANGS on this device family (observed as a
    subprocess timeout), so commands are passed directly as argv — never
    shell-wrapped. Prefer read_device_file() for reading app-private
    files (binary-safe via exec-out).
    """
    rc, out, err = run(
        "-s", serial, "shell", "run-as",
        SHUGOCORE_PACKAGE, *shell_args,
    )
    return rc, out, err


def read_device_file(serial: str, path: str,
                     max_bytes: int = 1 << 20) -> Optional[bytes]:
    """Read a device file under the package sandbox (binary-safe).

    Uses `exec-out run-as cat <path>`, which streams raw bytes without a
    shell wrapper (sh -c under run-as hangs on this device line). Returns
    None when the file is unreadable or the command is refused.
    """
    proc = subprocess.run(
        [ADB, "-s", serial, "exec-out", "run-as",
         SHUGOCORE_PACKAGE, "cat", path],
        capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout[:max_bytes]


def wait_for_agent_loop(serial: str, within_s: float = 30.0,
                        needle: str = "Decision made for task") -> bool:
    """Poll (fresh) logcat until the agent's 1 Hz decision loop is live.

    After `am start`, the Chaquopy runtime + Python agent can take
    seconds to re-initialise; injecting transcripts before the loop is up
    drops them (observation with no agent). Clears logcat first so only
    fresh evidence counts.
    """
    clear_logcat(serial)
    return expect_in_logs(serial, needle, within_s=within_s)


def inject_transcript(serial: str, text: str, wait_s: float = 1.5) -> None:
    """Send a transcript via the debug broadcast.

    The payload crosses `adb shell`, which word-splits every remote
    argument (no re-quoting) — a multi-word `--es text "…"` arrives as
    separate tokens and `am` misparses them (observed: the bare token "a"
    became the intent's package, and the broadcast was dropped). The
    transcript is therefore base64-encoded into the `text_b64` extra:
    ASCII-only, space-free, a single intact token end-to-end.
    """
    import base64
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    run(
        "-s", serial, "shell", "am", "broadcast", "-a", INJECT_ACTION,
        "--es", "text_b64", b64,
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
    ok = expect_in_logs(serial, "timer set for 2 seconds", within_s=ack_window())
    _log(logger, "  timer ack log line:", "FOUND" if ok else "MISSING")
    return ok


def _sep(logger: List[str]) -> None:
    _log(logger, "=" * 78)


def step_service_alive(serial: str, logger: List[str]) -> bool:
    """Low-level probe: adb shell, package resolvable, agent loop active.

    The on-device Python is a Chaquopy embedded runtime (no `python3`
    binary), so liveness is proven by the 1 Hz agent-loop decision line the
    engine emits to logcat under the `python.stderr` tag — never by trying
    to exec a python binary via run-as.
    """
    rc, out, err = run(
        "-s", serial, "shell", "ps", "|", "grep", "-i", "shugocore"
    )
    alive = rc == 0 and "shugocore" in out.lower()
    runtime_in_logs = bool(log_lines_containing(serial, "python.stderr"))
    loop_ok = expect_in_logs(serial, "Decision made for task", within_s=ack_window())
    for ln in [
        f"  ps grep shugocore: {'yes' if alive else 'no'}",
        f"  Chaquopy runtime (python.stderr in logcat): {'yes' if runtime_in_logs else 'no'}",
        f"  agent loop ('Decision made for task'): {'yes' if loop_ok else 'no'}",
    ]:
        _log(logger, ln)
    return alive and runtime_in_logs and loop_ok

def step_timer_fires_while_away(serial: str, logger: List[str]) -> bool:
    """Force-stop, then verify the expired timer is re-announced."""
    ensure_service_started(serial)
    # An 8 s timer guarantees expiry AFTER the force-stop: the 1-3 s spent
    # finding the ack plus the 10 s suspension always exceed its lifespan,
    # so restore() re-inserts it as fired_while_away on the next boot.
    inject_scanout(
        serial, "set a timer for eight seconds and tell me when it fires"
    )
    if not expect_in_logs(serial, "timer set for 8 seconds", within_s=ack_window()):
        _log_red(logger, "  timer ack missing before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued; waiting for timer expiry+restart")
    time.sleep(10.0)
    ensure_service_started(serial)
    ok = expect_in_logs(serial, "while you were away", within_s=restart_window())
    _log(logger, "  'while you were away' log line:", "FOUND" if ok else "MISSING")
    return ok

def step_fact_stores(serial: str, logger: List[str]) -> bool:
    """Assert an OS-level fact landed in SQLite."""
    inject_scanout(serial, "remember that my favorite color is midnight blue")
    ok = expect_in_logs(serial, "I'll remember", within_s=ack_window())
    _log(logger, "  \"I'll remember\" log line:", "FOUND" if ok else "MISSING")
    return ok


def step_memory_question(serial: str, logger: List[str]) -> bool:
    """Ask a memory question and confirm the answer uses memory content."""
    clear_logcat(serial)
    inject_transcript(serial, "remember that my favorite color is midnight blue")
    time.sleep(ack_window() * 0.5)
    before = set(log_lines_containing(serial, "midnight blue"))
    # "recall my favorite color" is a deterministic recall command whose
    # spoken reply carries the stored fact. ("remind me what my favorite
    # color is" would classify to the clarify handler and reply
    # "Remember what?" instead of the fact.)
    inject_transcript(serial, "recall my favorite color")
    time.sleep(ack_window() * 0.5)
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
    if not expect_in_logs(serial, "I'll remember", within_s=ack_window()):
        _log_red(logger, "  fact not stored before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued")
    time.sleep(2.0)
    ensure_service_started(serial)
    # The agent loop must be live before the recall can be answered — an
    # observation injected during re-initialisation is dropped ("no agent").
    if not wait_for_agent_loop(serial, within_s=restart_window()):
        _log_red(logger, "  agent loop did not resume after restart")
        return False
    inject_transcript(serial, "recall my favorite color")
    time.sleep(ack_window() * 0.5)
    ok = expect_in_logs(serial, "midnight blue", within_s=ack_window())
    _log(logger, "  fact survives restart:", "FOUND" if ok else "MISSING")
    return ok


def step_full_teardown_announced(serial: str, logger: List[str]) -> bool:
    """Full teardown round-trip: seed timers/facts, force-stop, restart,
    and confirm the agent announces what was restored.

    Ordering matters: the fact is seeded FIRST and the timer LAST, then the
    force-stop fires immediately after the timer ack.  With a real on-device
    model each ack costs one decision (~30-60 s); if the timer is seeded
    first, the 8 s timer expires while the agent is still alive, is consumed
    by the live loop, and the restart has nothing missed to announce.
    """
    ensure_service_started(serial)
    inject_scanout(serial, "remember that my middle name is June")
    # First ack after the cadence measurement: the measurement injects its own
    # transcripts, so this ack can queue behind 1-2 pending decisions.  On a
    # ~50 s cadence a single ack_window (2.5x cadence) is not enough; allow
    # 4 cadences before declaring the seed lost.
    first_ack = max(ack_window() * 2.0, _CADENCE_S * 4.0)
    if not expect_in_logs(serial, "I'll remember", within_s=first_ack):
        _log_red(logger, "  fact store ack missing before teardown")
        return False
    inject_scanout(serial, "set a timer for eight seconds")
    if not expect_in_logs(serial, "timer set for 8 seconds", within_s=ack_window()):
        _log_red(logger, "  timer ack missing before teardown")
        return False
    run("-s", serial, "shell", "am", "force-stop", SHUGOCORE_PACKAGE)
    _log(logger, "  force-stop issued; waiting for timer expiry + restart")
    time.sleep(10.0)
    ensure_service_started(serial)
    ok1 = expect_in_logs(serial, "while you were away", within_s=restart_window())
    _log(logger, "  'while you were away' after restart:", "FOUND" if ok1 else "MISSING")
    clear_logcat(serial)
    inject_transcript(serial, "what is my middle name")
    time.sleep(ack_window() * 0.5)
    ok2 = expect_in_logs(serial, "june", within_s=ack_window())
    _log(logger, "  restored fact (june) after restart:", "FOUND" if ok2 else "MISSING")
    return ok1 and ok2

def step_personality_model_genesis(serial: str, logger: List[str]) -> bool:
    """Verify the personality model JSON exists in app-private storage.

    The agent persists its living PersonalityModel atomically to
    data_dir/personality_model.json (json.dump layout: name/generation/
    policy/traits). We read it sandbox-only — never by path outside the
    confirmed app-private location.
    """
    path = f"/data/data/{SHUGOCORE_PACKAGE}/files/personality_model.json"
    data = read_device_file(serial, path, max_bytes=4096)
    body = (data or b"").decode("utf-8", "replace").strip()
    ok = (data is not None and len(body) > 60
          and '"generation"' in body and '"warmth"' in body)
    _log(logger, "  personality model JSON readable:", "OK" if ok else "MISSING/EMPTY")
    if not ok:
        _log(logger, "    first 400 chars:", body[:400])
    return ok

def step_personality_growth_log(serial: str, logger: List[str]) -> bool:
    """Grow the personality one generation on-device, then read the
    growth-driven model JSON back and diff it offline against the
    pre-growth snapshot.

    Growth fires when the agent's in-memory turn counter reaches
    GROWTH_EVERY (25 conversational turns) and is persisted atomically to
    data_dir/personality_model.json. We inject turns in a loop, re-reading
    the JSON every few injections until generation advances, then compute
    compare_models locally and assert drift > 0.
    """
    import ast
    import json as _json
    from personality import PersonalityModel

    def read_prod_model(serial: str) -> Optional[PersonalityModel]:
        path = (f"/data/data/{SHUGOCORE_PACKAGE}/files/"
                "personality_model.json")
        raw = read_device_file(serial, path)
        if raw is None:
            return None
        out = raw.decode("utf-8", "replace")
        data = None
        try:
            data = _json.loads(out)
        except Exception:
            try:
                data = ast.literal_eval(out)
            except Exception:
                return None
        try:
            return PersonalityModel.from_dict(data)
        except Exception:
            return None

    before = read_prod_model(serial)
    if before is None:
        _log_red(logger, "  cannot read personality model before growth")
        return False
    _log(logger, "  pre-growth generation:", before.generation)

    # v1.28.3: alternating-burst growth drive.
    #
    # The old praise-only mix saturates: after repeated smoke runs the
    # on-device model sits at generation N with warmth pinned at 1.0 (and
    # verbosity/humor possibly clamped too), so every praise delta clamps to
    # zero and compare_models reports drift 0.0 forever (NONE/STALE).
    #
    # Fix: alternate PRAISE / CRITICIZE bursts.  Deltas carry the net sign of
    # the current burst's explicit feedback (personality/growth.py
    # FEEDBACK_PATTERNS) and drift is unsigned RMS distance, so:
    #   - traits saturated at 1.0 move under the CRITICIZE burst
    #   - traits saturated at 0.0 move under the PRAISE burst
    #   - mid-range traits move under either
    # The initial burst direction is chosen from the live snapshot: whichever
    # direction has more total room across the trait vector.  The loop
    # recomputes compare_models against the ORIGINAL pre-growth snapshot
    # after every generation advance, passes on the first drift > 0, and
    # flips the pool between bursts so consecutive generations pull in
    # opposite directions.
    praise_turns = [
        # warmth+ (explicit praise)
        "thank you, that really helps",
        "you're the best companion I could ask for",
        "you're so kind to remember June",
        # verbosity+
        "tell me more, go on",
        # humor+
        "that's hilarious, you're funny",
        "good one, love your jokes",
        # questions -> curiosity, facts -> warmth, commands -> proactivity
        "what time is it",
        "what is my favorite color",
        "recall what you know about June",
        "remember that I like sunrise walks",
    ]
    criticize_turns = [
        # warmth-
        "you're so cold today",
        "how rude of you",
        "you're heartless",
        "you don't care about any of this",
        # verbosity-
        "be brief, less detail",
        "stop rambling, you're too long",
        "shorter answers please",
        # humor-
        "that was a bad joke, not funny",
        "you're cringe",
        "too silly",
    ]

    room_up = sum(1.0 - t.value for t in before.traits.values())
    room_down = sum(t.value for t in before.traits.values())
    start_praise = room_up >= room_down
    _log(logger, "  pre-growth room up/down:",
         f"{room_up:.2f}/{room_down:.2f} -> start",
         "PRAISE" if start_praise else "CRITICIZE", "burst")

    import os as _os
    total_window = float(_os.environ.get("SHUGOCORE_SMOKE_GROWTH_WINDOW_S", "1500"))
    drive_every = max(1.5, _CADENCE_S * 0.5)

    pool = praise_turns if start_praise else criticize_turns
    burst = 0
    t_start = time.time()
    idx = 0
    after = None
    ok = False
    while time.time() - t_start < total_window:
        inject_transcript(serial, pool[idx % len(pool)], wait_s=0.2)
        idx += 1
        if idx % 3 != 0:
            continue
        probe = read_prod_model(serial)
        if probe is not None and probe.generation > before.generation:
            after = probe
            try:
                comparison = PersonalityModel.compare_models(before, after)
                drift = comparison.get("drift")
            except Exception as exc:
                _log_red(logger, "  compare_models raised:", exc)
                drift = None
            if drift is not None and drift > 0.0:
                ok = True
                break
            # A generation advanced but every delta clamped to zero: flip
            # the burst emphasis and keep driving until the window expires.
            burst += 1
            pool = praise_turns if burst % 2 == 0 else criticize_turns
            _log(logger, "  drift still 0 after generation",
                 after.generation, "-> switching to",
                 "PRAISE" if burst % 2 == 0 else "CRITICIZE", "burst")
        time.sleep(drive_every)

    if not ok:
        _log_red(logger, "  growth drift did not advance from 0.0")
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
        "  comparison trait_deltas:",
        json.dumps(comparison.get("trait_deltas"), sort_keys=True, indent=2),
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

    # v1.28.2: sample the decision cadence once so phase windows tolerate
    # real on-device model latency (delegation fix).  Sampled BEFORE the
    # phases so service_alive + every window scale correctly.
    global _CADENCE_S
    _CADENCE_S = measure_decision_cadence(serial)
    _cad_log = f"measured decision cadence: {_CADENCE_S:.1f}s"
    print(_cad_log, flush=True)

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
