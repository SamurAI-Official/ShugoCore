"""Recursive loop training harness-parsing tests.

Regression guard for the multi-line JSON report emitted by
`android_device_smoke.py --json` (pretty-printed with indent=2).  The
training wrapper previously matched only single-line objects, so the
per-phase results were silently dropped and every round reported an empty
`results` list — hiding the failing phase entirely.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import recursive_loop_training as rlt


def _report(passed=9, total=9, results=None):
    return {
        "device": "serial",
        "total_phases": total,
        "passed": passed,
        "results": results if results is not None else [
            {"run": 1, "phase": "service_alive", "desc": "live", "ok": True},
        ],
    }


class ExtractHarnessJsonTest(unittest.TestCase):
    """extract_harness_json: parse the trailing pretty-printed report."""

    def test_parses_indented_multiline_report(self):
        """indent=2 output is the real harness format (previously missed)."""
        out = "PHASE: service_alive\n  => PASS\n" + json.dumps(
            _report(), indent=2)
        payload = rlt.extract_harness_json(out)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["passed"], 9)
        self.assertEqual(payload["total_phases"], 9)
        self.assertEqual(payload["results"][0]["phase"], "service_alive")

    def test_preserves_full_phase_list(self):
        phases = list(rlt.ALL_PHASES)
        results = [{"run": 1, "phase": p, "desc": "", "ok": True}
                   for p in phases]
        out = json.dumps(_report(results=results), indent=2)
        payload = rlt.extract_harness_json(out)
        self.assertEqual([r["phase"] for r in payload["results"]], phases)

    def test_tolerates_trailing_text(self):
        out = json.dumps(_report(passed=8), indent=2) + "\nleftover noise\n"
        self.assertEqual(rlt.extract_harness_json(out)["passed"], 8)

    def test_returns_none_without_json(self):
        self.assertIsNone(rlt.extract_harness_json(""))
        self.assertIsNone(rlt.extract_harness_json("PHASE: x\n  => FAIL"))

    def test_ignores_json_without_results_key(self):
        """A stray dict is not mistaken for the harness report."""
        out = json.dumps({"unrelated": True}, indent=2)
        self.assertIsNone(rlt.extract_harness_json(out))


class RunHarnessResultTest(unittest.TestCase):
    """run_harness: ok flag from exit code + parsed report."""

    def _run(self, rc, out):
        with mock.patch.object(rlt, "sh", return_value=(rc, out)):
            return rlt.run_harness("serial", None)

    def test_all_passed(self):
        ok, payload, _ = self._run(0, json.dumps(_report(), indent=2))
        self.assertTrue(ok)
        self.assertEqual(payload["passed"], payload["total_phases"])

    def test_phase_failure_reports_payload(self):
        results = [{"run": 1, "phase": "service_alive", "desc": "", "ok": True},
                   {"run": 1, "phase": "fact_survives_restart", "desc": "",
                    "ok": False}]
        out = json.dumps(_report(passed=1, total=2, results=results), indent=2)
        ok, payload, _ = self._run(1, out)
        self.assertFalse(ok)
        failed = [r["phase"] for r in payload["results"] if not r["ok"]]
        self.assertEqual(failed, ["fact_survives_restart"])

    def test_timeout_has_no_payload(self):
        ok, payload, _ = self._run(124, "(timeout)")
        self.assertFalse(ok)
        self.assertIsNone(payload)

    def test_clean_exit_without_json_still_ok(self):
        ok, payload, _ = self._run(0, "no json here")
        self.assertTrue(ok)
        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
