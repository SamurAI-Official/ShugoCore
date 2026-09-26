"""Tests for Track 1 heartbeats over the ShugoNet mesh.

The election itself was already wired to the Android DDS path, so heartbeats
could only travel between devices running the app's Kotlin layer: any Python-only
fleet reported ``standalone`` on every node, because the election had no peer
advertisements to compare against. The transport now carries them too.

Three things are pinned here:

* the wire contract (an advertisement reaches a peer, is consumed, is never
  acked, and malformed ones are ignored);
* the election outcomes that depend on it (lower priority wins, a thermally
  critical or headroomless node is ineligible, both sides agree on the primary);
* the agent wiring (``_mesh_heartbeat_payload`` / ``_mesh_heartbeat_received`` /
  ``_mesh_mem_headroom``), exercised without booting a full agent.

Ports are OS-assigned and every runtime is stopped in tearDown, so the suite is
safe to run alongside a live fleet.
"""
import socket
import time
import unittest
from unittest.mock import MagicMock

from agent_runtime import ShugonetAgentRuntime
from mesh_election import MeshElection, available_memory_bytes
from shugocore_agent import AndroidAgent

_COMFORTABLE_MEM = 1 << 30        # 1 GiB of advertised headroom


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate, timeout: float = 6.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class MeshHeartbeatTransportTestCase(unittest.TestCase):
    """Two real runtimes on loopback; the loop is off so beats are explicit."""

    def setUp(self):
        self.received_a = []
        self.received_b = []
        self.a = ShugonetAgentRuntime(agent_id="shugo-desktop",
                                      host="127.0.0.1", port=_free_port(),
                                      heartbeat_interval=0)
        self.b = ShugonetAgentRuntime(agent_id="shugo-mobile",
                                      host="127.0.0.1", port=_free_port(),
                                      heartbeat_interval=0)
        self.a.set_heartbeat_handler(self.received_a.append)
        self.b.set_heartbeat_handler(self.received_b.append)
        self.a.add_peer("shugo-mobile", "127.0.0.1", self.b.status()["port"])
        self.b.add_peer("shugo-desktop", "127.0.0.1", self.a.status()["port"])
        self.a.start()
        self.b.start()
        self.assertTrue(
            _wait_for(lambda: self.a.reconnect_peers() >= 1
                      and self.b.reconnect_peers() >= 1),
            "loopback peers did not connect")

    def tearDown(self):
        self.a.stop()
        self.b.stop()

    def test_explicit_heartbeat_reaches_the_peer(self):
        sent = self.a.broadcast_heartbeat(
            {"node_id": "shugo-desktop", "priority": 10, "thermal_status": 0,
             "mem_available_bytes": _COMFORTABLE_MEM})
        self.assertEqual(sent, 1)
        self.assertTrue(_wait_for(lambda: bool(self.received_b)),
                        "peer never received the advertisement")
        record = self.received_b[0]
        self.assertEqual(record["node_id"], "shugo-desktop")
        self.assertEqual(record["priority"], 10)
        self.assertIn("received_at", record)      # stamped by the transport
        self.assertEqual(self.b.status()["heartbeat"]["heard"], 1)
        self.assertEqual(
            self.b.heartbeat_snapshot()["shugo-desktop"]["node_id"],
            "shugo-desktop")
        self.assertEqual(self.a.status()["stats"].get("heartbeats_sent"), 1)
        self.assertEqual(self.b.status()["stats"].get("heartbeats_received"), 1)

    def test_provider_supplies_the_advertisement(self):
        self.a.set_heartbeat_provider(
            lambda: {"node_id": "shugo-desktop", "priority": 7,
                     "mem_available_bytes": _COMFORTABLE_MEM})
        self.assertTrue(self.a.status()["heartbeat"]["advertising"])
        self.assertEqual(self.a.broadcast_heartbeat(), 1)
        self.assertTrue(_wait_for(lambda: bool(self.received_b)))
        self.assertEqual(self.received_b[-1]["priority"], 7)

    def test_no_provider_means_no_advertisement(self):
        self.assertEqual(self.a.broadcast_heartbeat(), 0)
        self.assertEqual(self.a.broadcast_heartbeat({}), 0)
        self.assertFalse(self.a.status()["heartbeat"]["advertising"])
        self.assertEqual(self.received_b, [])

    def test_heartbeat_is_stamped_for_a_token_gated_mesh(self):
        """A token-gated peer rejects unstamped frames, so stamp the beat."""
        runtime = ShugonetAgentRuntime(agent_id="shugo-desktop",
                                       host="127.0.0.1", port=0,
                                       heartbeat_interval=0,
                                       auth_token="mesh-secret")
        sent = []
        runtime._outbound["peer"] = MagicMock()
        runtime._outbound["peer"].send.side_effect = (
            lambda message: sent.append(message) or True)
        runtime.set_heartbeat_provider(
            lambda: {"node_id": "shugo-desktop", "priority": 10,
                     "mem_available_bytes": _COMFORTABLE_MEM})
        self.assertEqual(runtime.broadcast_heartbeat(), 1)
        self.assertEqual(sent[0]["type"], "heartbeat")
        self.assertEqual(sent[0]["token"], "mesh-secret")
        self.assertEqual(sent[0]["payload"]["node_id"], "shugo-desktop")

    def test_provider_failure_is_contained(self):
        def _boom():
            raise RuntimeError("no election")

        self.a.set_heartbeat_provider(_boom)
        self.assertEqual(self.a.broadcast_heartbeat(), 0)


class HeartbeatDispatchTestCase(unittest.TestCase):
    """Wire-level behaviour of one inbound frame (no sockets)."""

    def setUp(self):
        self.runtime = ShugonetAgentRuntime(agent_id="shugo-desktop",
                                            host="127.0.0.1", port=0,
                                            heartbeat_interval=0)
        self.got = []
        self.runtime.set_heartbeat_handler(self.got.append)

    def test_heartbeat_is_consumed_without_acking(self):
        sock = MagicMock()
        self.runtime._dispatch_message(
            {"type": "heartbeat", "from": "shugo-mobile",
             "payload": {"node_id": "shugo-mobile", "priority": 500,
                         "thermal_status": 0, "mem_available_bytes": 1}}, sock)
        self.assertEqual(len(self.got), 1)
        sock.sendall.assert_not_called()

    def test_malformed_heartbeats_are_ignored(self):
        for bad in ({"type": "heartbeat"},
                    {"type": "heartbeat", "payload": "nope"},
                    {"type": "heartbeat", "payload": {"no_node_id": True}},
                    {"type": "heartbeat", "payload": []}):
            self.runtime._dispatch_message(bad, MagicMock())
        self.assertEqual(self.got, [])
        self.assertEqual(self.runtime.status()["heartbeat"]["heard"], 0)

    def test_handler_failure_never_escapes(self):
        def _boom(_payload):
            raise RuntimeError("election blew up")

        self.runtime.set_heartbeat_handler(_boom)
        self.runtime._dispatch_message(
            {"type": "heartbeat",
             "payload": {"node_id": "shugo-mobile", "mem_available_bytes": 1}},
            MagicMock())
        # Recorded regardless: the advertisement is data, the handler is policy.
        self.assertEqual(self.runtime.status()["heartbeat"]["heard"], 1)


class AgentHeartbeatWiringTestCase(unittest.TestCase):
    """The agent hooks, exercised without booting an agent.

    The methods are bound onto a stand-in object, so the test covers exactly the
    wiring that matters -- identity/priority/headroom in the advertisement, and
    the merge/de-dupe of inbound peers -- with no model backend, memory DB or
    mesh socket involved.
    """

    @staticmethod
    def _dummy(election):
        class _Dummy:
            _mesh_mem_headroom = AndroidAgent._mesh_mem_headroom
            _mesh_heartbeat_payload = AndroidAgent._mesh_heartbeat_payload
            _mesh_heartbeat_received = AndroidAgent._mesh_heartbeat_received
            _mesh_heartbeat_tick = AndroidAgent._mesh_heartbeat_tick
            _mesh_may_act = AndroidAgent._mesh_may_act
            engine = None

            def log(self, *args, **kwargs):
                pass

        obj = _Dummy()
        obj.mesh_election = election
        obj.telemetry = {}
        return obj

    def test_ingest_evaluates_the_election_immediately(self):
        """A peer's first advertisement must be candidate-visible at once.

        Waiting for the next tick left the role stale for minutes (a tick is
        model-bound), so a follower kept acting after its peers were visible.
        """
        obj = self._dummy(MeshElection("shugo-mobile", priority=500))
        obj._mesh_heartbeat_received(
            {"node_id": "shugo-desktop", "priority": 10, "thermal_status": 0,
             "mem_available_bytes": _COMFORTABLE_MEM})
        self.assertEqual([p["node_id"] for p in obj.mesh_election.live_peers()],
                         ["shugo-desktop"])
        self.assertEqual(obj.mesh_election.tick()["primary"], "shugo-desktop")
        self.assertFalse(obj._mesh_may_act("speak"))

    def test_payload_carries_identity_priority_and_real_headroom(self):
        obj = self._dummy(MeshElection("shugo-desktop", priority=10))
        payload = obj._mesh_heartbeat_payload()
        self.assertEqual(payload["node_id"], "shugo-desktop")
        self.assertEqual(payload["priority"], 10)
        # Regression: a zero figure here made every candidate ineligible, so the
        # node advertised itself out of its own election.
        if available_memory_bytes() > 0:
            self.assertGreater(payload["mem_available_bytes"], 0)
        self.assertEqual(obj.mesh_election.tick()["primary"], "shugo-desktop")

    def test_payload_is_empty_without_an_election(self):
        self.assertEqual(self._dummy(None)._mesh_heartbeat_payload(), {})

    def test_telemetry_memory_wins_over_the_host_measurement(self):
        obj = self._dummy(MeshElection("shugo-desktop", priority=10))
        obj.telemetry = {"mem_available_bytes": 12345}
        self.assertEqual(obj._mesh_mem_headroom(), 12345)

    def test_received_peer_is_merged_deduped_and_bounded(self):
        obj = self._dummy(MeshElection("shugo-desktop", priority=10))
        for _ in range(3):
            obj._mesh_heartbeat_received(
                {"node_id": "shugo-mobile", "priority": 500, "seq": 2,
                 "mem_available_bytes": _COMFORTABLE_MEM})
        peers = obj.telemetry["mesh_peers"]
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["device_id"], "shugo-mobile")
        self.assertEqual(peers[0]["source"], "mesh")
        # Our own advertisement is never merged back in as a peer.
        obj._mesh_heartbeat_received({"node_id": "shugo-desktop",
                                      "priority": 10})
        self.assertEqual(len(obj.telemetry["mesh_peers"]), 1)
        # Junk is ignored rather than stored.
        obj._mesh_heartbeat_received("nope")
        obj._mesh_heartbeat_received({"node_id": ""})
        self.assertEqual(len(obj.telemetry["mesh_peers"]), 1)


class ElectionOverMeshTestCase(unittest.TestCase):
    """Two elections wired through two runtimes agree on one primary."""

    def setUp(self):
        self.desktop = MeshElection("shugo-desktop", priority=10)
        self.mobile = MeshElection("shugo-mobile", priority=500)
        self.rt_d = ShugonetAgentRuntime(agent_id="shugo-desktop",
                                         host="127.0.0.1", port=_free_port(),
                                         heartbeat_interval=0)
        self.rt_m = ShugonetAgentRuntime(agent_id="shugo-mobile",
                                         host="127.0.0.1", port=_free_port(),
                                         heartbeat_interval=0)
        self.rt_d.set_heartbeat_provider(lambda: self.desktop.local_heartbeat(
            mem_available_bytes=_COMFORTABLE_MEM))
        self.rt_d.set_heartbeat_handler(self.desktop.observe_heartbeat)
        self.rt_m.set_heartbeat_provider(lambda: self.mobile.local_heartbeat(
            mem_available_bytes=_COMFORTABLE_MEM))
        self.rt_m.set_heartbeat_handler(self.mobile.observe_heartbeat)
        self.rt_d.add_peer("shugo-mobile", "127.0.0.1",
                           self.rt_m.status()["port"])
        self.rt_m.add_peer("shugo-desktop", "127.0.0.1",
                           self.rt_d.status()["port"])
        self.rt_d.start()
        self.rt_m.start()
        self.assertTrue(_wait_for(lambda: self.rt_d.reconnect_peers() >= 1
                                  and self.rt_m.reconnect_peers() >= 1))

    def tearDown(self):
        self.rt_d.stop()
        self.rt_m.stop()

    def test_both_sides_elect_the_lower_priority_host(self):
        self.assertEqual(self.rt_d.broadcast_heartbeat(), 1)
        self.assertEqual(self.rt_m.broadcast_heartbeat(), 1)
        self.assertTrue(
            _wait_for(lambda: len(self.desktop.live_peers()) == 1
                      and len(self.mobile.live_peers()) == 1),
            "heartbeats did not cross over the mesh")
        desktop = self.desktop.tick()
        mobile = self.mobile.tick()
        self.assertEqual(desktop["primary"], "shugo-desktop")
        self.assertEqual(mobile["primary"], "shugo-desktop")
        self.assertEqual(sorted(desktop["candidates"]),
                         ["shugo-desktop", "shugo-mobile"])
        self.assertEqual(self.desktop.role(), "primary")
        self.assertEqual(self.mobile.role(), "follower")
        self.assertTrue(self.desktop.is_primary())
        self.assertFalse(self.mobile.is_primary())


class AgentGuardTestCase(unittest.TestCase):
    """The primary-only guardrail, evaluated with real election state."""

    @staticmethod
    def _node(election):
        class _Dummy:
            _mesh_may_act = AndroidAgent._mesh_may_act

            def log(self, *args, **kwargs):
                pass

        obj = _Dummy()
        obj.mesh_election = election
        return obj

    @staticmethod
    def _with_peer(election, local_priority, peer_priority):
        peer_id = ("shugo-desktop" if election.node_id != "shugo-desktop"
                   else "shugo-mobile")
        election.observe_heartbeat({
            "node_id": election.node_id, "priority": local_priority,
            "mem_available_bytes": _COMFORTABLE_MEM})
        election.observe_heartbeat({
            "node_id": peer_id, "priority": peer_priority,
            "mem_available_bytes": _COMFORTABLE_MEM})
        return election

    def test_follower_refuses_and_lease_holder_allows(self):
        follower = self._node(self._with_peer(
            MeshElection("shugo-mobile", priority=500),
            local_priority=500, peer_priority=10))
        self.assertFalse(follower._mesh_may_act("speak"))
        holder = self._node(self._with_peer(
            MeshElection("shugo-desktop", priority=10),
            local_priority=10, peer_priority=500))
        self.assertTrue(holder._mesh_may_act("speak"))

    def test_lone_node_acts_standalone(self):
        election = MeshElection("shugo-desktop", priority=10)
        election.local_heartbeat(mem_available_bytes=_COMFORTABLE_MEM)
        self.assertTrue(self._node(election)._mesh_may_act("speak"))

    def test_missing_election_never_blocks(self):
        self.assertTrue(self._node(None)._mesh_may_act("speak"))


class ElectionEligibilityTestCase(unittest.TestCase):
    """The two exclusion rules that decide who may hold the lease."""

    def test_thermal_critical_node_loses_to_a_cool_peer(self):
        election = MeshElection("shugo-desktop", priority=10)
        election.observe_heartbeat({
            "node_id": "shugo-desktop", "priority": 10, "thermal_status": 3,
            "mem_available_bytes": _COMFORTABLE_MEM})
        election.observe_heartbeat({
            "node_id": "shugo-mobile", "priority": 500, "thermal_status": 0,
            "mem_available_bytes": _COMFORTABLE_MEM})
        self.assertEqual(election.tick()["primary"], "shugo-mobile")
        self.assertEqual(election._eligible_reasons(),
                         {"shugo-desktop": "thermal-critical"})
        self.assertEqual(election.role(), "follower")

    def test_headroomless_node_is_ineligible(self):
        """Regression: advertising mem=0 excluded every host by accident."""
        election = MeshElection("shugo-desktop", priority=10)
        election.local_heartbeat(thermal_status=0, mem_available_bytes=0)
        self.assertIsNone(election.tick()["primary"])
        self.assertEqual(election._eligible_reasons(),
                         {"shugo-desktop": "no-headroom"})

    def test_host_headroom_is_measured_when_the_platform_can(self):
        value = available_memory_bytes()
        if value == 0:
            self.skipTest("this platform cannot report free physical memory")
        self.assertGreater(value, 0)


if __name__ == "__main__":
    unittest.main()


