"""
Thermal behavior stress tests for AccelerationPolicy.

Drives the demotion ladders through sustained oscillation, combined
accelerator-failure + thermal degradation, concurrent updates, and the
audit-event contract. Kind-based assertions (npu/dsp/gpu/cpu), never
device display names.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acceleration import (AccelerationPolicy, AcceleratorDevice,
                          AcceleratorKind)


class _AuditSink:
    """Recording stand-in for the audit log."""

    def __init__(self):
        self.events = []
        self._lock = threading.Lock()

    def append(self, event_type, payload):
        with self._lock:
            self.events.append((str(event_type), dict(payload or {})))

    def count(self, event_type):
        with self._lock:
            return sum(1 for t, _ in self.events if t == event_type)


def _full_policy(audit=None):
    """Policy with an explicit npu/dsp/gpu set (CPU is ensured implicitly)."""
    return AccelerationPolicy(
        devices=[AcceleratorDevice("npu", "Hexagon NPU", "test"),
                 AcceleratorDevice("dsp", "DSP", "test"),
                 AcceleratorDevice("gpu", "Adreno GPU", "test")],
        audit=audit)


def _kinds(policy, workload):
    return [d.kind for d in policy.resolve(workload)]


class TestLadderTransitions(unittest.TestCase):
    def setUp(self):
        self.p = _full_policy()

    def test_level0_full_ladders(self):
        self.assertEqual(_kinds(self.p, "llm"),
                         [AcceleratorKind.NPU, AcceleratorKind.GPU,
                          AcceleratorKind.CPU])
        self.assertEqual(_kinds(self.p, "vision"),
                         [AcceleratorKind.NPU, AcceleratorKind.DSP,
                          AcceleratorKind.GPU, AcceleratorKind.CPU])

    def test_level1_demotes_gpu_only(self):
        self.p.apply_thermal_level(1)
        self.assertEqual(_kinds(self.p, "llm"),
                         [AcceleratorKind.NPU, AcceleratorKind.CPU])
        self.assertEqual(_kinds(self.p, "vision"),
                         [AcceleratorKind.NPU, AcceleratorKind.DSP,
                          AcceleratorKind.CPU])

    def test_level2_cpu_only(self):
        self.p.apply_thermal_level(2)
        self.assertEqual(_kinds(self.p, "llm"), [AcceleratorKind.CPU])
        self.assertEqual(_kinds(self.p, "vision"), [AcceleratorKind.CPU])
        self.assertEqual(_kinds(self.p, "general"), [AcceleratorKind.CPU])

    def test_oscillation_down_and_back(self):
        self.p.apply_thermal_level(2)
        self.assertEqual(_kinds(self.p, "llm"), [AcceleratorKind.CPU])
        self.p.apply_thermal_level(1)   # gpu still demoted, dsp returns
        self.assertEqual(_kinds(self.p, "llm"),
                         [AcceleratorKind.NPU, AcceleratorKind.CPU])
        self.assertIn(AcceleratorKind.DSP, _kinds(self.p, "vision"))
        self.p.apply_thermal_level(0)   # everything back
        self.assertIn(AcceleratorKind.GPU, _kinds(self.p, "vision"))
        self.assertIn(AcceleratorKind.GPU, _kinds(self.p, "llm"))

    def test_preferred_follows_top_of_ladder(self):
        self.assertIs(self.p.preferred("llm").kind, AcceleratorKind.NPU)
        self.p.apply_thermal_level(2)
        self.assertIs(self.p.preferred("llm").kind, AcceleratorKind.CPU)

    def test_clamped_and_coerced_inputs(self):
        self.assertEqual(self.p.apply_thermal_level("1"), 1)
        self.assertEqual(self.p.apply_thermal_level(1.9), 1)
        self.assertEqual(self.p.apply_thermal_level(99), 2)
        self.assertEqual(self.p.apply_thermal_level(-7), 0)
        self.assertEqual(self.p.apply_thermal_level(True), 1)

    def test_none_raises_callers_must_sanitize(self):
        with self.assertRaises(TypeError):
            self.p.apply_thermal_level(None)


class TestOscillationSoak(unittest.TestCase):
    def test_1000_thermal_cycles_no_drift(self):
        audit = _AuditSink()
        p = _full_policy(audit)
        for _ in range(1000):
            for level in (0, 1, 2, 1, 0):
                p.apply_thermal_level(level)
                self.assertEqual(p.thermal_level, level)
            kinds = _kinds(p, "llm")
            self.assertEqual(kinds[0], AcceleratorKind.NPU)
            self.assertEqual(kinds[-1], AcceleratorKind.CPU)
        # 4 transitions per cycle, audited exactly once each.
        self.assertEqual(audit.count("thermal_demotion"), 4000)

    def test_same_level_reapply_audits_nothing(self):
        audit = _AuditSink()
        p = _full_policy(audit)
        for _ in range(50):
            p.apply_thermal_level(1)
        self.assertEqual(audit.count("thermal_demotion"), 1)
        self.assertEqual(p.thermal_level, 1)


class TestCombinedDegradation(unittest.TestCase):
    def setUp(self):
        self.p = _full_policy()
        self.npu = AcceleratorDevice("npu", "Hexagon NPU", "test")
        self.gpu = AcceleratorDevice("gpu", "Adreno GPU", "test")
        self.cpu = AcceleratorDevice("cpu", "CPU", "implicit")

    def test_failure_demotes_across_thermal_levels(self):
        self.p.report_failure(self.npu, "driver crash")
        self.assertEqual(_kinds(self.p, "llm"),
                         [AcceleratorKind.GPU, AcceleratorKind.CPU])
        self.p.apply_thermal_level(1)   # gpu now thermally demoted
        self.assertEqual(_kinds(self.p, "llm"), [AcceleratorKind.CPU])
        self.p.apply_thermal_level(0)   # thermal clears; failure persists
        self.assertEqual(_kinds(self.p, "llm"),
                         [AcceleratorKind.GPU, AcceleratorKind.CPU])
        self.p.report_recovery("Hexagon NPU")
        self.assertEqual(_kinds(self.p, "llm"),
                         [AcceleratorKind.NPU, AcceleratorKind.GPU,
                          AcceleratorKind.CPU])

    def test_thermal_recovery_restores_what_failure_did_not(self):
        self.p.apply_thermal_level(2)
        self.p.apply_thermal_level(0)
        self.assertEqual(_kinds(self.p, "vision")[0], AcceleratorKind.NPU)

    def test_everything_failed_still_resolves_cpu(self):
        for dev in (self.npu, self.gpu, self.cpu):
            self.p.report_failure(dev, "cascading failure")
        ladder = self.p.resolve("llm")
        self.assertTrue(ladder, "resolve must never be empty")
        self.assertIs(ladder[0].kind, AcceleratorKind.CPU)

    def test_recovery_of_unknown_device_is_noop(self):
        self.p.report_recovery("never-existed")
        self.assertEqual(self.p.describe()["failed"], [])


class TestConcurrentThermalUpdates(unittest.TestCase):
    def test_8_threads_hammering_levels(self):
        p = _full_policy()
        errors = []

        def hammer(tid):
            try:
                for i in range(250):
                    p.apply_thermal_level((tid + i) % 3)
                    ladder = p.resolve("llm")
                    self.assertTrue(ladder)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(t,))
                   for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
        self.assertEqual(errors, [])
        self.assertIn(p.thermal_level, (0, 1, 2))

    def test_concurrent_failures_and_thermal(self):
        p = _full_policy()
        devices = [AcceleratorDevice("npu", f"npu-{i}", "test")
                   for i in range(4)]

        def fail_some():
            for d in devices:
                p.report_failure(d, "stress")

        def thermal():
            for i in range(300):
                p.apply_thermal_level(i % 3)

        threads = [threading.Thread(target=fail_some),
                   threading.Thread(target=thermal),
                   threading.Thread(target=thermal)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        snap = p.describe()
        self.assertEqual(snap["failed"],
                         sorted(d.name for d in devices))
        self.assertTrue(p.resolve("llm"))


class TestAuditAndSnapshot(unittest.TestCase):
    def test_describe_snapshot_reflects_state(self):
        audit = _AuditSink()
        p = _full_policy(audit)
        p.apply_thermal_level(1)
        p.report_failure(AcceleratorDevice("gpu", "Adreno GPU", "test"), "x")
        snap = p.describe()
        self.assertEqual(snap["thermal_level"], 1)
        self.assertEqual(snap["failed"], ["Adreno GPU"])
        self.assertEqual(len(snap["devices"]), 4)   # npu, dsp, gpu, cpu
        kinds = {d["kind"] for d in snap["devices"]}
        self.assertEqual(kinds, {"npu", "dsp", "gpu", "cpu"})

    def test_audit_failure_is_swallowed(self):
        class _Boom:
            def append(self, *_):
                raise RuntimeError("audit disk full")

        p = _full_policy(_Boom())
        self.assertEqual(p.apply_thermal_level(2), 2)
        p.report_failure(AcceleratorDevice("gpu", "g", "test"), "x")
        self.assertEqual(p.thermal_level, 2)

    def test_failure_audit_payload(self):
        audit = _AuditSink()
        p = _full_policy(audit)
        p.report_failure(AcceleratorDevice("gpu", "Adreno GPU", "test"),
                         "kernel panic")
        events = [e for e in audit.events if e[0] == "accelerator_demoted"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[1 - 1][1]["device"], "Adreno GPU")
        self.assertEqual(events[0][1]["kind"], "gpu")


if __name__ == "__main__":
    unittest.main()
