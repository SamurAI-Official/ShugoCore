"""Tests for agent_runtime.py — ShugoNet TCP/JSON transport.

Safe, fast, non-hanging tests: no real TCP connections, no port binding.
Uses the classes directly with verify-only assertions.
"""
import unittest
from unittest.mock import patch, MagicMock

from agent_runtime import ShugonetAgentRuntime, _PeerConnection


class TestShugonetAgentRuntime(unittest.TestCase):
    """Tests ShugonetAgentRuntime without starting a real server."""

    def setUp(self):
        self.runtime = ShugonetAgentRuntime(
            agent_id="test-agent", host="127.0.0.1", port=18720)
        # Don't call start() — we test the API contract directly

    def test_list_agents_initial(self):
        """Should list only this agent when no peers exist."""
        agents = self.runtime.list_agents()
        self.assertEqual(agents, ["test-agent"])

    def test_add_peer(self):
        """Should register a peer."""
        self.runtime.add_peer("peer1", "127.0.0.1", 9001)
        agents = self.runtime.list_agents()
        self.assertIn("peer1", agents)

    def test_remove_peer(self):
        """Should remove a peer."""
        self.runtime.add_peer("peer1", "127.0.0.1", 9001)
        self.runtime.remove_peer("peer1")
        agents = self.runtime.list_agents()
        self.assertNotIn("peer1", agents)

    def test_status_before_start(self):
        """Should return status dict before start."""
        status = self.runtime.status()
        self.assertEqual(status["agent_id"], "test-agent")
        self.assertFalse(status["running"])

    def test_send_to_unknown_peer(self):
        """Should refuse send to unknown peer."""
        result = self.runtime.send("nonexistent", "topic", {"data": 1})
        self.assertEqual(result["status"], "refused")

    def test_query_returns_empty_list(self):
        """Should return empty list when no peers."""
        results = self.runtime.query("anything")
        self.assertEqual(results, [])

    def test_sync_refuses_no_peers(self):
        """Should refuse sync when no peers."""
        result = self.runtime.sync()
        self.assertEqual(result["status"], "refused")

    def test_shugonet_bridge_list_agents(self):
        """ShugonetExecutionHandler can list agents."""
        from shugonet_bridge import ShugonetExecutionHandler
        handler = ShugonetExecutionHandler(self.runtime)
        result = handler.handle({
            "action_type": "network_list_agents",
            "params": {},
        })
        self.assertEqual(result["status"], "success")
        self.assertIn("test-agent", result["agents"])

    def test_shugonet_bridge_send_refused(self):
        """ShugonetExecutionHandler send to unknown peer."""
        from shugonet_bridge import ShugonetExecutionHandler
        handler = ShugonetExecutionHandler(self.runtime)
        result = handler.handle({
            "action_type": "network_send",
            "params": {"peer": "nobody", "topic": "t", "payload": {}},
        })
        self.assertIn("status", result)

    def test_shugonet_bridge_query_refused(self):
        """ShugonetExecutionHandler query with no peers."""
        from shugonet_bridge import ShugonetExecutionHandler
        handler = ShugonetExecutionHandler(self.runtime)
        result = handler.handle({
            "action_type": "network_query",
            "params": {"query": "test", "peers": []},
        })
        self.assertIn("status", result)

    def test_shugonet_bridge_status(self):
        """ShugonetExecutionHandler status."""
        from shugonet_bridge import ShugonetExecutionHandler
        handler = ShugonetExecutionHandler(self.runtime)
        result = handler.handle({
            "action_type": "network_status",
            "params": {},
        })
        self.assertEqual(result["status"], "success")


class TestPeerConnection(unittest.TestCase):
    """Tests _PeerConnection without real sockets."""

    def test_send_before_connect(self):
        """Should handle send before connect."""
        conn = _PeerConnection("test", "127.0.0.1", 19999)
        self.assertFalse(conn.send({"type": "ping"}))

    def test_close_idempotent(self):
        """Close on unconnected connection should not raise."""
        conn = _PeerConnection("test", "127.0.0.1", 19999)
        conn.close()  # should not raise
        self.assertFalse(conn.connected)

    def test_connected_property_default(self):
        """Connected property should be False before connect."""
        conn = _PeerConnection("test", "127.0.0.1", 19999)
        self.assertFalse(conn.connected)


if __name__ == "__main__":
    unittest.main()