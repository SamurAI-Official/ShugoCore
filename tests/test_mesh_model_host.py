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
# Two devices small enough that the layer budget spreads across both: the plan fills
# the largest headroom first, so a device with room for every layer is used alone.
PEER_SMALL = {"node_id": "shugo-a51", "mem_available_bytes": 300 * MIB,
              "thermal_status": 0}
PEER_MEDIUM = {"node_id": "shugo-a16", "mem_available_bytes": 500 * MIB,
               "thermal_status": 0}


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
        # A port nothing can be listening on, and a port probe that is never called:
        # the pre-launch guard asks whether something already answers on our port, and
        # a unit test must not open a socket (urlopen to a dead port is seconds each
        # when a proxy is configured).
        kwargs.setdefault("port", 1)
        kwargs.setdefault("port_serving", lambda _base: False)
        # Never wait on a socket in a unit test either: with an injected no-op sleep
        # this loop spins against real time, so the 120 s default is a busy wait.
        kwargs.setdefault("health_timeout", 0)
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

    def _splitting_host(self, healthy=True, **kwargs):
        peer = {"node_id": "shugo-a16", "mem_available_bytes": 2000 * 1024 * 1024,
                "thermal_status": 0}
        # The peer map must name the same device the plan assigned, or the layers
        # are dropped for having no dialable endpoint.
        kwargs.setdefault("peers", [("shugo-a16", "127.0.0.1", 9000)])
        kwargs.setdefault("health", lambda _base: healthy)
        return self._host(live_peers=[peer], health_timeout=0, **kwargs)

    def test_a_launched_split_that_never_answers_is_not_reported_as_a_split(self):
        """Intent is not a mode: verify it, or say what really happened."""
        host = self._splitting_host(healthy=False)
        state = host.start()
        self.assertEqual(state["mode"], "split-unhealthy")
        self.assertFalse(state["health"])
        self.assertIn("did not answer /health", state["reason"])

    def test_a_port_owned_by_a_stale_host_is_caught_before_launching(self):
        """Otherwise the health check answers from a model that is not ours."""
        host = self._splitting_host(port_serving=lambda _base: True)
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


class DifferenceTestCase(unittest.TestCase):
    """The diff that decides whether a running split has to change."""

    def test_a_device_that_lost_its_layers_is_a_detach(self):
        diff = mmh.split_difference({"shugo-a51": 5}, {"shugo-tab": 5})
        self.assertTrue(diff["changed"])
        self.assertEqual(diff["detach"], ["shugo-a51"])
        self.assertEqual(diff["attach"], ["shugo-tab"])

    def test_a_layer_count_change_is_a_resize(self):
        diff = mmh.split_difference({"shugo-tab": 5}, {"shugo-tab": 8})
        self.assertEqual((diff["detach"], diff["attach"]), ([], []))
        self.assertEqual(diff["resize"], ["shugo-tab"])
        self.assertEqual(diff["remote_delta"], 3)

    def test_the_same_split_is_not_a_change(self):
        diff = mmh.split_difference({"shugo-tab": 5}, {"shugo-tab": 5})
        self.assertFalse(diff["changed"])


class DegradeTestCase(unittest.TestCase):
    """Restarting with fewer layers rather than not at all."""

    def test_the_weakest_device_gives_up_its_layers_first(self):
        step = mmh.degrade_split({"shugo-mac": 10, "shugo-a16": 3},
                                 {"shugo-mac": 1900 * MIB, "shugo-a16": 100 * MIB})
        self.assertEqual(step["dropped"], "shugo-a16")
        self.assertEqual(step["assignments"], {"shugo-mac": 10})

    def test_with_nothing_remote_left_it_says_so(self):
        step = mmh.degrade_split({}, {})
        self.assertEqual(step["assignments"], {})
        self.assertIsNone(step["dropped"])
        self.assertIn("nothing remote", step["reason"])


class _Fleet:
    """A launcher the test can kill, and a clock the test can advance."""

    def __init__(self):
        self.now = 1000.0
        self.healthy = True
        self.launchers = []

    def clock(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def launcher(self, *_args, **_kwargs):
        instance = _FakeLauncher()
        self.launchers.append(instance)
        return instance

    @property
    def current(self):
        return self.launchers[-1] if self.launchers else None

    def die(self):
        """The host model process goes away, as one did on the bench."""
        if self.current is not None:
            self.current.started = False


class ReconcileTestCase(unittest.TestCase):
    """A plan is not a one-off: the fleet changes while the model is running.

    The first live cross-device run missed a phone by one second and then hosted the
    model locally for the rest of the process's life; a device that goes critical has
    to give its layers up; and a host model that stops serving has to come back.
    """

    def setUp(self):
        self.fleet = _Fleet()
        self.peers = []

    def _start(self, peers, **kwargs):
        self.peers = [dict(peer) for peer in peers]
        host = mmh.MeshModelHost(
            "model.gguf", binary="llama-server",
            port=1, health_timeout=1.0, settle_timeout=0,
            launcher=self.fleet.launcher,
            connect=lambda *_a, **_k: _Conn(),
            prober=lambda *_a, **_k: True,
            sleep=self.fleet.advance,
            clock=self.fleet.clock,
            health=lambda _base: self.fleet.healthy,
            port_serving=lambda _base: False,
            live_peers=lambda: [dict(peer) for peer in self.peers],
            peers=lambda: [(peer["node_id"], "127.0.0.1", 9000)
                           for peer in self.peers],
            **kwargs)
        # A 20 MiB layer without a 480 MiB file on disk: the plan only needs the size.
        host.bytes_per_layer = lambda: 20 * MIB
        return host

    def test_a_device_that_arrives_late_is_given_layers(self):
        """The failure that started P1.2: the phone heartbeated one second too late."""
        host = self._start([PEER_PHONE])
        first = host.start()
        self.assertEqual(first["mode"], "split")
        self.assertTrue(first["assignments"])
        self.peers.append(PEER_AMPLE)
        self.assertEqual(host.reconcile()["action"], "deferred")  # must stick
        self.fleet.advance(host.cooldown_s + 1)
        self.assertEqual(host.reconcile()["action"], "restart")
        self.assertIn("shugo-mac", host.status()["assignments"])
        self.assertEqual(host.status()["relaunches"], 1)

    def test_a_healthy_split_is_left_alone(self):
        host = self._start([PEER_PHONE])
        host.start()
        self.assertEqual(host.reconcile()["action"], "hold")
        self.assertEqual(len(self.fleet.launchers), 1)      # no restart, no reload

    def test_a_device_that_goes_critical_gives_up_its_layers(self):
        hot = dict(PEER_PHONE, node_id="shugo-a51")
        host = self._start([hot])
        self.assertTrue(host.start()["assignments"])
        self.peers[0] = dict(hot, thermal_status=4)
        host.reconcile()
        self.fleet.advance(host.cooldown_s + 1)
        self.assertEqual(host.reconcile()["action"], "restart")
        state = host.status()
        self.assertEqual(state["assignments"], {})
        self.assertEqual(state["mode"], "local")
        self.assertIn("thermal_status=4", state["reason"])

    def test_a_detached_device_is_not_given_layers_straight_back(self):
        """THERMAL_REFUSE_STATUS is a runtime detach, not only a planning rule."""
        hot = dict(PEER_PHONE, node_id="shugo-a51")
        host = self._start([hot])
        host.start()
        self.peers[0] = dict(hot, thermal_status=4)
        host.reconcile()
        self.fleet.advance(host.cooldown_s + 1)
        host.reconcile()
        self.assertEqual(host.status()["assignments"], {})
        # It advertises itself cool again: the pin must still hold it out, so the plan
        # is unchanged and there is nothing to do.
        self.peers[0] = dict(hot, thermal_status=0)
        self.assertEqual(host.reconcile()["action"], "hold")
        self.assertEqual(host.status()["assignments"], {})
        # ... and once the pin has expired it is usable again.
        self.fleet.advance(host.thermal_pin_s + 1)
        self.assertEqual(host.reconcile()["action"], "deferred")
        self.fleet.advance(host.cooldown_s + 1)
        self.assertEqual(host.reconcile()["action"], "restart")
        self.assertIn("shugo-a51", host.status()["assignments"])

    def test_a_host_model_that_died_is_restarted(self):
        host = self._start([PEER_PHONE])
        host.start()
        self.fleet.die()
        result = host.reconcile()
        self.assertEqual(result["action"], "restart")
        self.assertIn("died", result["cause"])
        self.assertTrue(host.status()["health"])
        self.assertEqual(host.status()["relaunches"], 1)

    def test_restarts_give_up_layers_until_the_model_runs_locally(self):
        """Keep trying somewhere: fewer remote layers beat no model at all."""
        host = self._start([PEER_SMALL, PEER_MEDIUM])
        first = host.start()
        self.assertEqual(len(first["assignments"]), 2)
        self.fleet.healthy = False
        counts = [sum(first["assignments"].values())]
        actions = []
        for _ in range(4):
            self.fleet.advance(host.cooldown_s + 1)
            actions.append(host.reconcile()["action"])
            counts.append(sum(host.status()["assignments"].values()))
        self.assertEqual(counts, sorted(counts, reverse=True))   # never grows
        self.assertEqual(counts[-1], 0)                          # ends up local
        # "failed" is the honest end: it gave up only after trying with no remote
        # layers at all, so there is no model running to describe as local.
        self.assertEqual(host.status()["mode"], "failed")
        self.assertIn("gave_up", actions)
        # ... and giving up is final: it stops trying instead of looping.
        self.assertEqual(host.reconcile()["action"], "hold")


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
