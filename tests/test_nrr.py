"""NRR contract shim tests: descriptor/result schema, protocol envelopes,
transport adapter routing + fail-closed behavior, and the peripheral worker
stub.  Offline only -- no NRR binary, no devices.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobile_nodes import (MobileComputeBroker, MobileNodeRegistry,
                          node_supports_workload)
from policy import CapabilityRegistry
from ros2_interface import StubROS2Interface

import nrr.protocol as proto
from nrr.adapter import NRRTransportAdapter
from nrr.descriptor import NRRFrameDescriptor
from nrr.result import (NRRRenderResult, describe_frame,
                        make_not_supported_result)


def _registry_with_node(device="pixel8", workloads=("nrr_render",)):
    reg = MobileNodeRegistry()
    reg.pair(device, {"compute_caps": {"fp16": True, "vram_mb": 4096,
                                       "workloads": list(workloads)}})
    return reg


class DescriptorSchemaTest(unittest.TestCase):
    def test_round_trip(self):
        d = describe_frame("f1", 640, 480, model_id="m",
                           reference_id="r", frame_index=7,
                           delta_time=0.033, quality_hint="quality")
        back = NRRFrameDescriptor.from_dict(d)
        self.assertEqual(back.frame_id, "f1")
        self.assertEqual(back.frame_index, 7)
        self.assertEqual(back.schema_version, 1)

    def test_bad_resolution_rejected(self):
        with self.assertRaises(ValueError):
            describe_frame("f", 0, 480)
        with self.assertRaises(ValueError):
            describe_frame("f", 9000, 480)

    def test_bad_pixel_format_rejected(self):
        with self.assertRaises(ValueError):
            describe_frame("f", 640, 480, pixel_format="CMYK")

    def test_bad_quality_hint_rejected(self):
        with self.assertRaises(ValueError):
            describe_frame("f", 640, 480, quality_hint="ultra")

    def test_missing_frame_id_rejected(self):
        with self.assertRaises(ValueError):
            describe_frame("", 640, 480)

    def test_from_dict_revalidates(self):
        with self.assertRaises(ValueError):
            NRRFrameDescriptor.from_dict({"frame_id": "f", "width": -1,
                                          "height": 10})


class ResultSchemaTest(unittest.TestCase):
    def test_not_supported_envelope(self):
        r = make_not_supported_result("f9", "no backend")
        self.assertEqual(r["status"], "not_supported")
        self.assertEqual(r["frame_id"], "f9")
        back = NRRRenderResult.from_dict(r)
        self.assertEqual(back.status, "not_supported")

    def test_ok_result_round_trip(self):
        r = NRRRenderResult(frame_id="f2", status="ok",
                            output_handle="buf://7", width=640,
                            height=480).to_dict()
        back = NRRRenderResult.from_dict(r)
        self.assertEqual(back.output_handle, "buf://7")
        self.assertEqual(back.width, 640)


class ProtocolTest(unittest.TestCase):
    def test_topic_shape(self):
        self.assertEqual(proto.nrr_topic("pixel8", proto.TOPIC_RENDER),
                         "/shugocore/mobile/pixel8/nrr/render")

    def test_render_request_envelope(self):
        d = describe_frame("f3", 320, 240)
        msg = proto.make_render_request("req-1", d)
        self.assertEqual(proto.msg_type(msg), "NRRRender")
        self.assertEqual(msg["descriptor"]["frame_id"], "f3")

class AdapterRoutingTest(unittest.TestCase):
    def _adapter(self, workloads=("nrr_render",), with_broker=True):
        reg = _registry_with_node(workloads=workloads)
        caps = CapabilityRegistry({"mobile_devices_allowlist": ["pixel8"]})
        broker = None
        if with_broker:
            broker = MobileComputeBroker(StubROS2Interface(), reg, caps)
        return NRRTransportAdapter(reg, broker=broker), reg

    def test_capable_nodes_lists_advertised(self):
        adapter, _ = self._adapter()
        ids = [n["device_id"] for n in adapter.capable_nodes()]
        self.assertEqual(ids, ["pixel8"])

    def test_capable_nodes_empty_without_advertisement(self):
        adapter, _ = self._adapter(workloads=("vision",))
        self.assertEqual(adapter.capable_nodes(), [])

    def test_render_refused_unpaired(self):
        adapter, _ = self._adapter()
        out = adapter.render("stranger", describe_frame("f", 64, 64))
        self.assertEqual(out["status"], "refused")
        self.assertIn("paired", out["reason"])

    def test_render_refused_no_capability(self):
        adapter, _ = self._adapter(workloads=("vision",))
        out = adapter.render("pixel8", describe_frame("f", 64, 64))
        self.assertEqual(out["status"], "refused")
        self.assertIn("nrr_render", out["reason"])

    def test_render_refused_invalid_descriptor(self):
        adapter, _ = self._adapter()
        out = adapter.render("pixel8", {"frame_id": "", "width": 0,
                                        "height": 0})
        self.assertEqual(out["status"], "refused")
        self.assertIn("descriptor", out["reason"])

    def test_render_refused_without_broker(self):
        adapter, _ = self._adapter(with_broker=False)
        out = adapter.render("pixel8", describe_frame("f", 64, 64))
        self.assertEqual(out["status"], "refused")

    def test_render_dispatches_through_broker(self):
        adapter, _ = self._adapter()
        out = adapter.render("pixel8", describe_frame("f4", 64, 64),
                             timeout=0.1)
        self.assertEqual(out["status"], "error")
        self.assertIn("timeout", out["reason"])

    def test_handle_result_happy_path(self):
        adapter, _ = self._adapter()
        envelope = proto.make_render_result(
            "req-1", make_not_supported_result("f5", "stub"))
        out = adapter.handle_result("pixel8", envelope)
        self.assertIsNotNone(out)
        self.assertEqual(out["frame_id"], "f5")

    def test_handle_result_refused_unpaired(self):
        adapter, _ = self._adapter()
        envelope = proto.make_render_result(
            "req-1", make_not_supported_result("f5", "stub"))
        self.assertIsNone(adapter.handle_result("stranger", envelope))

    def test_handle_result_refused_bad_type(self):
        adapter, _ = self._adapter()
        self.assertIsNone(adapter.handle_result("pixel8", {"type": "Nope"}))

    def test_worker_stub_answers_not_supported(self):
        out = NRRTransportAdapter.worker_stub(
            {"request_id": "r1", "descriptor": describe_frame("f6", 32, 32)})
        self.assertEqual(proto.msg_type(out), "NRRResult")
        self.assertEqual(out["result"]["status"], "not_supported")

    def test_worker_stub_rejects_invalid_descriptor(self):
        out = NRRTransportAdapter.worker_stub(
            {"request_id": "r1", "descriptor": {"frame_id": ""}})
        self.assertEqual(out["result"]["status"], "not_supported")


class CapabilityRoutingTest(unittest.TestCase):
    def test_nodes_for_workload_filters(self):
        reg = MobileNodeRegistry()
        reg.pair("gpu_node", {"compute_caps": {"workloads": ["nrr_render"]}})
        reg.pair("plain_node", {"sensors": ["camera"]})
        ids = [n["device_id"] for n in reg.nodes_for_workload("nrr_render")]
        self.assertEqual(ids, ["gpu_node"])

    def test_nodes_for_unknown_workload_empty(self):
        reg = MobileNodeRegistry()
        reg.pair("gpu_node", {"compute_caps": {"workloads": ["nrr_render"]}})
        self.assertEqual(reg.nodes_for_workload("teleport"), [])

    def test_manifest_caps_survive_pairing(self):
        reg = MobileNodeRegistry()
        entry = reg.pair("gpu_node", {"compute_caps": {"fp16": True,
                                                       "vram_mb": 3072,
                                                       "workloads": ["nrr_render",
                                                                     "vision"]}})
        caps = entry["manifest"]["compute_caps"]
        self.assertTrue(caps["fp16"])
        self.assertEqual(caps["vram_mb"], 3072)
        self.assertIn("nrr_render", caps["workloads"])

    def test_unknown_workload_names_dropped(self):
        reg = MobileNodeRegistry()
        entry = reg.pair("node", {"compute_caps": {"workloads": ["nrr_render",
                                                                  "missiles"]}})
        self.assertNotIn("missiles",
                         entry["manifest"]["compute_caps"].get("workloads", []))

    def test_node_supports_workload_helper(self):
        self.assertTrue(node_supports_workload(
            {"compute_caps": {"workloads": ["vision"]}}, "vision"))
        self.assertFalse(node_supports_workload({}, "vision"))
        self.assertFalse(node_supports_workload(
            {"compute_caps": {"workloads": ["vision"]}}, "nrr_render"))


class SnapshotBudgetTest(unittest.TestCase):
    """16 KB budget: structured payloads fit, unbounded blobs still fail."""

    def test_medium_structured_payload_accepted(self):
        from mobile_nodes import MobileNodeManager
        reg = MobileNodeRegistry()
        reg.pair("pixel8")
        caps = CapabilityRegistry({"mobile_devices_allowlist": ["pixel8"]})
        mgr = MobileNodeManager(StubROS2Interface(), reg, caps)
        payload = {"descriptor": describe_frame("fbig", 1920, 1080,
                                                model_id="m" * 100),
                   "padding": "x" * 8000}
        record = mgr.ingest("pixel8", "camera", payload)
        self.assertIsNotNone(record)

    def test_huge_payload_still_refused(self):
        from mobile_nodes import MobileNodeManager
        reg = MobileNodeRegistry()
        reg.pair("pixel8")
        caps = CapabilityRegistry({"mobile_devices_allowlist": ["pixel8"]})
        mgr = MobileNodeManager(StubROS2Interface(), reg, caps)
        self.assertIsNone(mgr.ingest("pixel8", "camera",
                                     {"blob": "x" * 20000}))


class TemporalSkipGateTest(unittest.TestCase):
    def test_first_observation_regenerates(self):
        from attention_layer import AttentionLayer
        layer = AttentionLayer()
        regen, reason = layer.should_regenerate({"human": {}})
        self.assertTrue(regen)
        self.assertEqual(reason, "first_observation")

    def test_unchanged_observation_skips(self):
        from attention_layer import AttentionLayer
        layer = AttentionLayer()
        obs = {"human": {"speech_source": "none", "face_count": 0,
                         "speech_recent": ""},
               "attention": {"attention_state": "unknown"},
               "scene_verdict": "", "mesh_peer_count": 0}
        self.assertTrue(layer.should_regenerate(obs)[0])
        layer.mark_regenerated(obs)
        regen, reason = layer.should_regenerate(obs)
        self.assertFalse(regen)
        self.assertEqual(reason, "observation_unchanged")

    def test_changed_observation_regenerates(self):
        from attention_layer import AttentionLayer
        layer = AttentionLayer()
        obs = {"human": {"speech_source": "none", "face_count": 0,
                         "speech_recent": ""},
               "attention": {"attention_state": "unknown"}}
        layer.mark_regenerated(obs)
        obs2 = dict(obs)
        obs2["human"] = {"speech_source": "none", "face_count": 1,
                         "speech_recent": ""}
        regen, reason = layer.should_regenerate(obs2)
        self.assertTrue(regen)
        self.assertEqual(reason, "observation_changed")

    def test_fresh_speech_forces_regeneration(self):
        from attention_layer import AttentionLayer
        layer = AttentionLayer()
        obs = {"human": {"speech_source": "none", "face_count": 1,
                         "speech_recent": ""},
               "attention": {"attention_state": "unknown"}}
        layer.mark_regenerated(obs)
        obs2 = {"human": {"speech_source": "person_talking", "face_count": 1,
                          "speech_recent": "hey shugo turn left"},
                "attention": {"attention_state": "attending"}}
        regen, reason = layer.should_regenerate(obs2)
        self.assertTrue(regen)
        self.assertEqual(reason, "fresh_signals")


if __name__ == "__main__":
    unittest.main()