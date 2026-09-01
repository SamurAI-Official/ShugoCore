"""
Tests for the hardware acceleration abstraction.

All enumerators are exercised with injected probes/fakes - no real device
access is required; the suite is deterministic on every platform.
"""

import os
import unittest
from unittest import mock

from acceleration import (
    AccelerationPolicy,
    AcceleratorDevice,
    AcceleratorKind,
    WorkloadClass,
    detect_platform,
    enumerate_android,
    enumerate_linux,
    enumerate_generic,
)


class TestAcceleratorDevice(unittest.TestCase):
    def test_kind_coercion_valid(self):
        d = AcceleratorDevice("npu", "Hexagon")
        self.assertEqual(d.kind, AcceleratorKind.NPU)

    def test_kind_coercion_invalid_falls_back_to_cpu(self):
        d = AcceleratorDevice("quantum", "QPU")
        self.assertEqual(d.kind, AcceleratorKind.CPU)

    def test_to_dict(self):
        d = AcceleratorDevice("gpu", "Adreno", source="android")
        self.assertEqual(d.to_dict()["kind"], "gpu")
        self.assertEqual(d.to_dict()["source"], "android")


class TestDetectPlatform(unittest.TestCase):
    def test_termux_prefix_wins(self):
        env = {"PREFIX": "/data/data/com.termux/files/usr"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("acceleration.os.path.exists", return_value=True):
            self.assertEqual(detect_platform(), "termux")

    def test_android_app_process(self):
        env = {"ANDROID_ART_ROOT": "/data/app", "PREFIX": "/usr"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch("acceleration.os.path.exists", return_value=True):
            self.assertEqual(detect_platform(), "android")

    def test_darwin(self):
        with mock.patch.dict(os.environ, {"PREFIX": "", "ANDROID_ART_ROOT": ""},
                             clear=False), \
                mock.patch("acceleration.os.path.exists", return_value=False), \
                mock.patch("acceleration.sys.platform", "darwin"):
            self.assertEqual(detect_platform(), "darwin")


class TestPolicyLadders(unittest.TestCase):
    def _policy(self, kinds=("npu", "gpu")):
        return AccelerationPolicy(
            devices=[AcceleratorDevice(k, f"dev-{k}") for k in kinds])

    def test_cpu_always_ensured(self):
        policy = self._policy()
        kinds = [d.kind for d in policy.devices()]
        self.assertIn(AcceleratorKind.CPU, kinds)

    def test_llm_ladder_order(self):
        policy = self._policy(("gpu", "npu"))
        resolved = policy.resolve("llm")
        self.assertEqual([d.kind for d in resolved],
                         [AcceleratorKind.NPU, AcceleratorKind.GPU,
                          AcceleratorKind.CPU])

    def test_vision_ladder_includes_dsp(self):
        policy = self._policy(("dsp", "gpu", "npu"))
        resolved = policy.resolve(WorkloadClass.VISION.value)
        self.assertEqual(resolved[0].kind, AcceleratorKind.NPU)
        self.assertIn(AcceleratorKind.DSP, [d.kind for d in resolved])

    def test_unknown_workload_uses_general(self):
        policy = self._policy(("npu",))
        resolved = policy.resolve("telepathy")
        self.assertEqual(resolved[0].kind, AcceleratorKind.NPU)

    def test_resolve_never_empty_even_if_all_fail(self):
        policy = self._policy(("npu", "gpu", "cpu"))
        for device in list(policy.devices()):
            policy.report_failure(device, "test")
        resolved = policy.resolve("llm")
        self.assertEqual([d.kind for d in resolved], [AcceleratorKind.CPU])

    def test_failure_demotion_and_recovery(self):
        policy = self._policy(("npu", "gpu"))
        npu = policy.preferred("llm")
        self.assertEqual(npu.kind, AcceleratorKind.NPU)
        policy.report_failure(npu, "driver crash")
        self.assertEqual(policy.preferred("llm").kind, AcceleratorKind.GPU)
        policy.report_recovery(npu.name)
        self.assertEqual(policy.preferred("llm").kind, AcceleratorKind.NPU)

    def test_thermal_level_1_demotes_gpu(self):
        policy = self._policy(("npu", "gpu"))
        policy.apply_thermal_level(1)
        kinds = [d.kind for d in policy.resolve("llm")]
        self.assertNotIn(AcceleratorKind.GPU, kinds)
        self.assertIn(AcceleratorKind.NPU, kinds)

    def test_thermal_level_2_is_cpu_only(self):
        policy = self._policy(("npu", "gpu", "dsp"))
        policy.apply_thermal_level(2)
        kinds = [d.kind for d in policy.resolve("vision")]
        self.assertEqual(kinds, [AcceleratorKind.CPU])

    def test_thermal_level_clamped(self):
        policy = self._policy()
        self.assertEqual(policy.apply_thermal_level(9), 2)
        self.assertEqual(policy.apply_thermal_level(-3), 0)

    def test_describe_snapshot(self):
        policy = self._policy(("npu",))
        policy.apply_thermal_level(1)
        snapshot = policy.describe()
        self.assertIn("platform", snapshot)
        self.assertEqual(snapshot["thermal_level"], 1)
        self.assertTrue(any(d["kind"] == "cpu" for d in snapshot["devices"]))

class _FakeAcceleratorBridge:
    """Kotlin bridge stand-in for the enumeration contract."""

    def __init__(self, payload=None, fail=False):
        self.payload = payload
        self.fail = fail

    def enumerateAccelerators(self):
        if self.fail:
            raise RuntimeError("bridge crashed")
        return self.payload


class TestEnumerators(unittest.TestCase):
    def test_android_bridge_good_payload(self):
        bridge = _FakeAcceleratorBridge([
            {"kind": "npu", "name": "Hexagon HTB", "details": {"soc": "8Gen3"}},
            {"kind": "gpu", "name": "Adreno 750"},
        ])
        devices = enumerate_android(bridge)
        kinds = [d.kind for d in devices]
        self.assertIn(AcceleratorKind.NPU, kinds)
        self.assertIn(AcceleratorKind.GPU, kinds)
        self.assertEqual(devices[0].details["soc"], "8Gen3")

    def test_android_bridge_garbage_coerced(self):
        devices = enumerate_android(_FakeAcceleratorBridge(
            ["not-a-dict", {"kind": "quantum", "name": "QPU"}, None]))
        self.assertTrue(all(isinstance(d, AcceleratorDevice) for d in devices))
        self.assertTrue(all(d.kind is AcceleratorKind.CPU for d in devices))

    def test_android_bridge_crash_is_safe(self):
        devices = enumerate_android(_FakeAcceleratorBridge(fail=True))
        self.assertEqual(devices, [])

    def test_linux_probes(self):
        hits = {"/dev/kgsl-3d0", "/dev/fastrpc"}
        devices = enumerate_linux(probe=lambda p: p in hits)
        kinds = {d.kind: d.name for d in devices}
        self.assertIn(AcceleratorKind.GPU, kinds)
        self.assertIn(AcceleratorKind.DSP, kinds)
        self.assertNotIn(AcceleratorKind.NPU, kinds)  # no Jetson/NPU probes hit

    def test_linux_jetson_via_probe_flag(self):
        # The device-tree read fails on the test host; NPU detection through
        # the accel subsystem probe is the injectable path.
        devices = enumerate_linux(probe=lambda p: p == "/dev/accel/accel0")
        self.assertTrue(any(d.kind is AcceleratorKind.NPU and "Intel" in d.name
                            for d in devices))

    def test_generic_is_empty(self):
        self.assertEqual(enumerate_generic(), [])

    def test_detect_on_generic_platform_appends_cpu(self):
        policy = AccelerationPolicy()
        with mock.patch("acceleration.detect_platform", return_value="windows"):
            devices = policy.detect()
        self.assertEqual([d.kind for d in devices], [AcceleratorKind.CPU])


if __name__ == "__main__":
    unittest.main()

