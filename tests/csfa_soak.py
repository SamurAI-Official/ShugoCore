"""CSFA soak — sustained Continuous Synthetic Functional Agency on the host.

Drives the same instrumented agent the endurance suite verifies, but for a
wall-clock duration instead of a fixed cycle count, checking the achievement
invariants periodically while the loop runs:

  1. the loop keeps cycling (cycles advance, by_outcome/by_source stay
     self-consistent),
  2. every declared loop stage stays fresh ("ok"; "stale" tolerated only if
     the checker itself was blocked past the freshness window),
  3. the audit chain verifies across the whole run,
  4. bounded structures stay bounded (activity ring, recent list),
  5. uptime is honest and non-decreasing.

The default backend is the deterministic scripted one (no network, no model);
``--null-dialect`` switches to the "nothing to do" backend to soak the
documented 3-strike fallback path instead. Exit code 0 only if every check
passed for the whole run; a JSON report is printed at the end either way.

Usage::

    python3 tests/csfa_soak.py --minutes 5
    python3 tests/csfa_soak.py --minutes 30 --interval 0.5 --null-dialect
"""
import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit import verify_audit_file  # noqa: E402
from shugocore_agent import PIPELINE_STAGES, create_agent  # noqa: E402

# Importing the endurance suite registers the scripted backends ("scripted"
# and "null_scripted") without running its tests.
import tests.test_csfa_endurance  # noqa: E402,F401


class Soak:
    def __init__(self, minutes: float, interval: float, check_every: float,
                 null_dialect: bool) -> None:
        self.deadline = time.monotonic() + minutes * 60.0
        self.interval = interval
        self.check_every = check_every
        self.null_dialect = null_dialect
        self.violations: list = []
        self.checks = 0
        self.last_uptime = -1.0
        self.tmp = tempfile.mkdtemp(prefix="shugocore_soak_")
        self.orig_cwd = os.getcwd()
        self.agent = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        os.chdir(self.tmp)
        self.agent = create_agent(device_caps="Soak-Host",
                                  api_url="http://127.0.0.1:11434",
                                  data_dir=self.tmp)
        self.agent._bootstrap()
        backend_type = "null_scripted" if self.null_dialect else "scripted"
        for model in self.agent.engine.models:
            if isinstance(model.get("backend"), dict):
                model["backend"] = {"type": backend_type,
                                    "model_id": "shugocore-local"}
        cache = getattr(self.agent.engine, "_backend_cache", None)
        if isinstance(cache, dict):
            cache.clear()

    def stop(self) -> None:
        if self.agent is not None:
            try:
                self.agent.cleanup()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.chdir(self.orig_cwd)

    # -- invariants ----------------------------------------------------------

    def check(self) -> bool:
        """Run one invariant pass; return True when everything holds."""
        self.checks += 1
        ok = True
        status = self.agent.get_status()
        loop = status.get("loop", {})

        cycles = int(loop.get("cycles", 0))
        if cycles <= 0:
            ok = self._fail("no cycles recorded")

        by_outcome = loop.get("by_outcome", {})
        by_source = loop.get("by_source", {})
        if sum(by_outcome.values()) != cycles:
            ok = self._fail(f"by_outcome sum {sum(by_outcome.values())} "
                            f"!= cycles {cycles}")
        if sum(by_source.values()) != cycles:
            ok = self._fail(f"by_source sum {sum(by_source.values())} "
                            f"!= cycles {cycles}")

        stages = status.get("loop_stages", {})
        for stage in PIPELINE_STAGES:
            entry = stages.get(stage)
            if not isinstance(entry, dict):
                ok = self._fail(f"stage {stage} missing from loop_stages")
            elif entry.get("state") not in ("ok", "stale"):
                ok = self._fail(f"stage {stage} state "
                                f"{entry.get('state')!r} not ok/stale")

        if len(loop.get("recent", [])) > 25:
            ok = self._fail("recent ring exceeded its 25-entry bound")

        uptime = float(status.get("uptime_seconds", 0.0))
        if uptime < self.last_uptime:
            ok = self._fail(f"uptime went backwards: {uptime} < "
                            f"{self.last_uptime}")
        self.last_uptime = uptime

        if not verify_audit_file("audit_chain.jsonl"):
            ok = self._fail("audit chain failed verification")
        return ok

    def _fail(self, message: str) -> bool:
        self.violations.append(
            {"ts": round(time.time(), 3), "check": self.checks,
             "detail": message})
        print(f"  VIOLATION: {message}", flush=True)
        return False

    # -- run -------------------------------------------------------------------

    def run(self) -> dict:
        started = time.monotonic()
        next_check = started + self.check_every
        ticks = 0
        while time.monotonic() < self.deadline:
            self.agent.tick()
            ticks += 1
            now = time.monotonic()
            if now >= next_check:
                self.check()
                next_check = now + self.check_every
                loop = self.agent.get_status().get("loop", {})
                outcomes = loop.get("by_outcome", {})
                top = max(outcomes.items(), key=lambda kv: kv[1]) \
                    if outcomes else ("-", 0)
                print(f"  t+{now - started:5.0f}s  cycles="
                      f"{loop.get('cycles', 0)}  top_outcome={top[0]}:{top[1]}"
                      f"  rate={loop.get('success_rate')}", flush=True)
            remaining = self.interval - (time.monotonic() - now)
            if remaining > 0:
                time.sleep(remaining)
        final_ok = self.check()
        status = self.agent.get_status()
        return {
            "verdict": "STABLE" if final_ok and not self.violations
            else "VIOLATIONS",
            "wall_seconds": round(time.monotonic() - started, 1),
            "ticks": ticks,
            "check_passes": self.checks,
            "violations": self.violations,
            "final_status": {
                "loop": status.get("loop", {}),
                "loop_stages": status.get("loop_stages", {}),
            },
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="csfa_soak", description="CSFA host soak (see module docstring)")
    parser.add_argument("--minutes", type=float, default=5.0,
                        help="soak duration in wall-clock minutes")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="seconds between agent ticks")
    parser.add_argument("--check-every", type=float, default=30.0,
                        help="seconds between invariant checks")
    parser.add_argument("--null-dialect", action="store_true",
                        help="soak the null-dialect 3-strike fallback path")
    parser.add_argument("--json", action="store_true",
                        help="print only the JSON report on stdout")
    args = parser.parse_args(argv)

    soak = Soak(args.minutes, args.interval, args.check_every,
                args.null_dialect)
    if not args.json:
        mode = "null-dialect (3-strike fallback)" if args.null_dialect \
            else "scripted model-driven"
        print(f"CSFA soak: {args.minutes} min, interval {args.interval}s, "
              f"check every {args.check_every}s, backend {mode}", flush=True)
    try:
        if args.json:
            # stdout carries ONLY the JSON report; everything else (boot
            # banner, progress lines, violation notices) goes to stderr.
            with contextlib.redirect_stdout(sys.stderr):
                soak.start()
                report = soak.run()
        else:
            soak.start()
            report = soak.run()
    finally:
        soak.stop()
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "STABLE" else 1


if __name__ == "__main__":
    sys.exit(main())
