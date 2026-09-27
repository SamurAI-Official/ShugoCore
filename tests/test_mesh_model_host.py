"""The mesh model host: plan, wake peripherals, launch, verify.

`plan_layer_split()` existed for a while and was called by nobody; these tests
pin the runtime that uses it -- what the plan becomes as llama.cpp arguments, what
happens when a peripheral never answers, and that "local" always says why.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from unittest import mock

import mesh_model_host as mmh  # noqa: E402

MIB = 1024 * 1024
PEER_AMPLE = {"node_id": "shugo-mac", "mem_available_bytes": 4 * 1024 * MIB,
              "thermal_status": 0}
PEER_PHONE = {"node_id": "shugo-tab", "mem_available_bytes": 400 * MIB,
              "thermal_status": 0}
PEER_HOT = {"node_id": "shugo-a51", "mem_available_bytes": 900 * MIB,
            "thermal_status": 4}


class PlanTestCase(unittest.TestCase):
    def test_layers_go_to_the_node_with_measured_headroom(self):
        plan = mmh.plan_for_fleet([PEER_AMPLE, PEER_PHONE], 24, 20 * MIB)
        self.assertIn("shugo-mac", plan["assignments"])
        self.assertGreater(plan["assignments"]["shugo-mac"], 0)
        self.assertLessEqual(plan["remote_layers"], 24)

    def test_a_thermally_critical_peer_is_skipped_with_a_reason(self):
        plan = mmh.plan_for_fleet([PEER_HOT], 24, 20 * MIB)
        self.assertEqual(plan["assignments"], {})
        reasons = " ".join(item["reason"] for item in plan["skipped"])
        self.assertIn("thermal_status=4", reasons)

    def test_a_phone_with_no_usable_headroom_is_skipped(self):
        tiny = {"node_id": "shugo-a16", "mem_available_bytes": 10 * MIB,
                "thermal_status": 0}
        plan = mmh.plan_for_fleet([tiny], 24, 20 * MIB)
        self.assertEqual(plan["assignments"], {})
        self.assertIn("insufficient_headroom",
                      " ".join(i["reason"] for i in plan["skipped"]))

    def test_an_unpaired_peer_is_skipped(self):
        unpaired = dict(PEER_AMPLE, paired=False)
        plan = mmh.plan_for_fleet([unpaired], 24, 20 * MIB)
        self.assertEqual(plan["assignments"], {})


    def test_an_excluded_device_is_left_out_with_a_reason(self):
        """The machine you are working on should not be asked to hold layers."""
        plan = mmh.plan_for_fleet([PEER_AMPLE, PEER_PHONE], 24, 20 * MIB,
                                  exclude=["shugo-mac"])
        self.assertNotIn("shugo-mac", plan["assignments"])
        reasons = " ".join(item["reason"] for item in plan["skipped"])
        self.assertIn("excluded by operator", reasons)


class _FakeLauncher:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True
        return True

    def running(self):
        return self.started

    def stop(self):
        self.started = False


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class SettleTestCase(unittest.TestCase):
    """A node that just booted has a verdict before it has peers.

    Without a bounded wait, planning reports "no peripheral" and the hive hosts the
    model locally for ever -- the same trap the operator --say hit.
    """

    def _host(self, **kwargs):
        kwargs.setdefault("peers", [("shugo-tab", "127.0.0.1", 9000)])
        # A port nothing can be listening on: the pre-launch guard asks whether the
        # host port is already serving another process, and on a machine with a
        # stray llama-server a shared default port makes this class order-dependent.
        kwargs.setdefault("port", 1)
        return mmh.MeshModelHost(
            "model.gguf", binary="llama-server",
            launcher=lambda *_a, **_k: _FakeLauncher(),
            connect=lambda *_a, **_k: _Conn(),
            prober=lambda *_a, **_k: True, sleep=lambda _s: None, **kwargs)

    def test_an_explicit_peer_list_is_never_made_to_wait(self):
        self.assertFalse(self._host(live_peers=[])._should_wait_for_peers({}))

    def test_no_peers_and_no_explicit_list_waits_for_the_mesh(self):
        self.assertTrue(self._host()._should_wait_for_peers({}))

    def test_a_plan_that_skipped_peers_is_an_answer_not_an_absence(self):
        """A decision is a decision: only an incomplete hive keeps us waiting."""
        host = self._host(live_peers=lambda: [], peers=[("shugo-tab", "h", 9000)])
        self.assertTrue(host._should_wait_for_peers({}))
        self.assertFalse(host._should_wait_for_peers({"assignments": {"tab": 2}}))

    def test_a_solo_node_is_not_late_and_must_not_be_delayed(self):
        host = self._host(live_peers=lambda: [], peers=[])
        self.assertFalse(host._should_wait_for_peers({}))

    def test_a_peer_that_has_been_heard_is_a_hive_even_with_an_empty_map(self):
        """Discovery fills the peer map over time; a heartbeat proves the hive."""
        host = self._host(live_peers=lambda: [{"node_id": "shugo-mac"}], peers=[])
        self.assertTrue(host._should_wait_for_peers({}))

    def test_a_zero_settle_timeout_disables_the_wait(self):
        self.assertFalse(
            self._host(settle_timeout=0)._should_wait_for_peers({}))

    def test_start_fails_closed_to_local_when_nobody_can_take_layers(self):
        tiny = {"node_id": "shugo-a51", "mem_available_bytes": 10 * 1024 * 1024,
                "thermal_status": 0}
        host = self._host(live_peers=[tiny])
        state = host.start()
        self.assertEqual(state["mode"], "local")
        self.assertIn("insufficient_headroom", state["reason"])

    def _splitting_host(self, **kwargs):
        peer = {"node_id": "shugo-a16", "mem_available_bytes": 2000 * 1024 * 1024,
                "thermal_status": 0}
        # The peer map must name the same device the plan assigned, or the layers
        # are dropped for having no dialable endpoint.
        kwargs.setdefault("peers", [("shugo-a16", "127.0.0.1", 9000)])
        return self._host(live_peers=[peer], health_timeout=0, **kwargs)

    def test_a_launched_split_that_never_answers_is_not_reported_as_a_split(self):
        """Intent is not a mode: verify it, or say what really happened."""
        host = self._splitting_host()
        with mock.patch.object(mmh, "health_ok", return_value=False):
            state = host.start()
        self.assertEqual(state["mode"], "split-unhealthy")
        self.assertFalse(state["health"])
        self.assertIn("did not answer /health", state["reason"])

    def test_a_port_owned_by_a_stale_host_is_caught_before_launching(self):
        """Otherwise the health check answers from a model that is not ours."""
        host = self._splitting_host()
        with mock.patch.object(mmh, "health_ok", return_value=True):
            state = host.start()
        self.assertFalse(state["launched"])
        self.assertIn("already serving another process", state["reason"])


    def test_the_plan_reports_what_the_planner_was_given(self):
        """`insufficient_headroom` is only actionable with the number behind it."""
        plan = mmh.plan_for_fleet([PEER_PHONE], 24, 20 * MIB,
                                  reserve_bytes=192 * MIB)
        self.assertEqual(plan["nodes"][0]["device_id"], "shugo-tab")
        self.assertEqual(plan["nodes"][0]["mem_available_bytes"],
                         PEER_PHONE["mem_available_bytes"])
        self.assertEqual(plan["bytes_per_layer"], 20 * MIB)
        self.assertEqual(plan["reserve_bytes"], 192 * MIB)


class EndpointTestCase(unittest.TestCase):
    def test_peers_give_ids_to_host_port(self):
        mapping = mmh.endpoint_map([("shugo-tab", "192.168.1.164", 9000)])
        self.assertEqual(mapping, {"shugo-tab": "192.168.1.164:9000"})

    def test_the_rpc_port_overrides_the_transport_port_on_the_same_host(self):
        """The peer map carries the transport port; the model dials the RPC port.

        Mixing them up is invisible -- the transport port is open too, so a
        reachability check passes and then the model cannot offload.
        """
        mapping = mmh.endpoint_map([("shugo-tab", "192.168.1.164", 9000)],
                                   port=50052)
        self.assertEqual(mapping, {"shugo-tab": "192.168.1.164:50052"})

    def test_the_override_applies_to_the_env_fallback_too(self):
        mapping = mmh.endpoint_map(None, env={
            "SHUGOCORE_MESH_PEERS": "shugo-mac=192.168.1.162:9000"}, port=50052)
        self.assertEqual(mapping, {"shugo-mac": "192.168.1.162:50052"})

    def test_the_operator_env_is_the_fallback(self):
        mapping = mmh.endpoint_map(None, env={
            "SHUGOCORE_MESH_PEERS": "shugo-mac=192.168.1.162:9000,bad-entry"})
        self.assertEqual(mapping, {"shugo-mac": "192.168.1.162:9000"})

    def test_reachability_is_a_connect_not_an_assumption(self):
        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        self.assertTrue(mmh.endpoint_reachable(
            "127.0.0.1:50052", connect=lambda *_a, **_k: _Conn()))

        def _refuse(*_a, **_k):
            raise OSError("connection refused")

        self.assertFalse(mmh.endpoint_reachable("127.0.0.1:50052",
                                                connect=_refuse))
        self.assertFalse(mmh.endpoint_reachable("not-an-endpoint",
                                                connect=_refuse))


class HostArgsTestCase(unittest.TestCase):
    def test_local_mode_still_carries_context_and_threads(self):
        args = mmh.host_extra_args({}, {}, context=2048, threads=4, mmap=True)
        self.assertEqual(args, ["-c", "2048", "-t", "4"])

    def test_one_peripheral_needs_no_tensor_split(self):
        args = mmh.host_extra_args({"shugo-mac": 12},
                                   {"shugo-mac": "10.0.0.2:50052"},
                                   context=1024, mmap=False)
        self.assertIn("--rpc", args)
        self.assertIn("10.0.0.2:50052", args)
        self.assertEqual(args[args.index("-dev") + 1], "RPC0")
        self.assertEqual(args[args.index("-ngl") + 1], "12")
        self.assertNotIn("-ts", args)
        self.assertIn("--no-mmap", args)

    def test_two_peripherals_state_their_split(self):
        args = mmh.host_extra_args(
            {"shugo-mac": 12, "shugo-tab": 4},
            {"shugo-mac": "10.0.0.2:50052", "shugo-tab": "10.0.0.3:50052"},
            context=0)
        self.assertEqual(args[args.index("-dev") + 1], "RPC0,RPC1")
        self.assertEqual(args[args.index("-ngl") + 1], "16")
        self.assertEqual(args[args.index("-ts") + 1], "12,4")

    def test_a_peer_without_an_endpoint_is_left_out(self):
        args = mmh.host_extra_args({"shugo-mac": 12, "shugo-ghost": 5},
                                   {"shugo-mac": "10.0.0.2:50052"}, context=0)
        self.assertEqual(args[args.index("-ngl") + 1], "12")


if __name__ == "__main__":
    unittest.main()
