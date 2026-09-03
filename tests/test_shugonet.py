"""
ShugoCore Shogunet integration tests
=====================================

Tests for the Shogunet networking bridge integration, following the
same pattern as test_mobile.py and test_robotics.py.
"""

import unittest
from unittest.mock import MagicMock, patch

from policy import (
    KNOWN_ACTION_TYPES,
    NETWORK_ACTION_TYPES,
    NETWORK_READ_ACTION_TYPES,
)
from shugonet_bridge import (
    ShugonetExecutionHandler,
    attach_network_fallbacks,
    network_topic,
    register_network_handlers,
)


class MockShugonetAgent:
    """Mock Shogunet agent runtime for testing."""

    def __init__(self):
        self.sent_messages = []
        self.queries = []
        self.syncs = []

    def send(self, peer, topic, payload):
        self.sent_messages.append({"peer": peer, "topic": topic, "payload": payload})
        return {"status": "success", "via": "tcp"}

    def query(self, query, peers=None, top_k=None):
        self.queries.append({"query": query, "peers": peers, "top_k": top_k})
        return [{"fact": "test fact", "salience": 1.0}]

    def sync(self, peer=None, since=None):
        self.syncs.append({"peer": peer, "since": since})
        return {"status": "success", "peer": peer or "*"}

    def list_agents(self):
        return ["agent-001", "agent-002"]

    def status(self):
        return {
            "agent_id": "test-agent",
            "running": True,
            "stats": {"sent": 0, "received": 0, "errors": 0},
        }


class TestNetworkActionTypes(unittest.TestCase):
    """Test that network action types are properly registered."""

    def test_network_action_types_in_known_types(self):
        """Network action types should be in KNOWN_ACTION_TYPES."""
        for action_type in NETWORK_ACTION_TYPES:
            self.assertIn(action_type, KNOWN_ACTION_TYPES)

    def test_network_read_action_types_in_known_types(self):
        """Network read action types should be in KNOWN_ACTION_TYPES."""
        for action_type in NETWORK_READ_ACTION_TYPES:
            self.assertIn(action_type, KNOWN_ACTION_TYPES)

    def test_network_action_types_defined(self):
        """Network action types should be properly defined."""
        self.assertEqual(
            NETWORK_ACTION_TYPES,
            {"network_send", "network_query", "network_sync"},
        )

    def test_network_read_action_types_defined(self):
        """Network read action types should be properly defined."""
        self.assertEqual(
            NETWORK_READ_ACTION_TYPES,
            {"network_list_agents", "network_status"},
        )


class TestNetworkTopic(unittest.TestCase):
    """Test network topic construction."""

    def test_network_topic_basic(self):
        """Should construct proper namespace topic."""
        topic = network_topic("agent-001", "task")
        self.assertEqual(topic, "/shugunet/agent-001/task")

    def test_network_topic_with_slash(self):
        """Should handle tail with leading slash."""
        topic = network_topic("agent-001", "/task")
        self.assertEqual(topic, "/shugunet/agent-001/task")


class TestShugonetExecutionHandler(unittest.TestCase):
    """Test the ShugonetExecutionHandler class."""

    def setUp(self):
        self.agent = MockShugonetAgent()
        self.handler = ShugonetExecutionHandler(self.agent)

    def test_handle_network_send(self):
        """Should dispatch network_send to agent.send()."""
        decision = {
            "action_type": "network_send",
            "params": {
                "peer": "agent-002",
                "topic": "/shugunet/agent-001/task",
                "payload": {"action": "scan"},
            },
        }
        result = self.handler.handle(decision)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["peer"], "agent-002")
        self.assertEqual(len(self.agent.sent_messages), 1)

    def test_handle_network_send_missing_params(self):
        """Should refuse network_send with missing params."""
        decision = {
            "action_type": "network_send",
            "params": {"peer": "agent-002"},
        }
        result = self.handler.handle(decision)
        self.assertEqual(result["status"], "refused")

    def test_handle_network_query(self):
        """Should dispatch network_query to agent.query()."""
        decision = {
            "action_type": "network_query",
            "params": {"query": "obstacle in zone north"},
        }
        result = self.handler.handle(decision)
        self.assertEqual(result["status"], "success")
        self.assertIn("results", result)
        self.assertEqual(len(self.agent.queries), 1)

    def test_handle_network_query_missing_params(self):
        """Should refuse network_query with missing query."""
        decision = {
            "action_type": "network_query",
            "params": {},
        }
        result = self.handler.handle(decision)
        self.assertEqual(result["status"], "refused")

    def test_handle_network_sync(self):
        """Should dispatch network_sync to agent.sync()."""
        decision = {
            "action_type": "network_sync",
            "params": {"peer": "agent-002"},
        }
        result = self.handler.handle(decision)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.agent.syncs), 1)

    def test_handle_network_list_agents(self):
        """Should dispatch network_list_agents to agent.list_agents()."""
        decision = {
            "action_type": "network_list_agents",
            "params": {},
        }
        result = self.handler.handle(decision)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["agents"], ["agent-001", "agent-002"])

    def test_handle_network_status(self):
        """Should dispatch network_status to agent.status()."""
        decision = {
            "action_type": "network_status",
            "params": {},
        }
        result = self.handler.handle(decision)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["agent_id"], "test-agent")

    def test_handle_unknown_action(self):
        """Should refuse unknown action types."""
        decision = {
            "action_type": "unknown_action",
            "params": {},
        }
        result = self.handler.handle(decision)
        self.assertEqual(result["status"], "refused")
        self.assertIn("unknown network action", result["reason"])


class TestRegisterNetworkHandlers(unittest.TestCase):
    """Test handler registration with execution layer."""

    def test_register_handlers(self):
        """Should register all network action types."""
        execution_layer = MagicMock()
        agent = MockShugonetAgent()

        register_network_handlers(execution_layer, agent)

        # Should register 5 handlers (3 action + 2 read)
        self.assertEqual(execution_layer.register_handler.call_count, 5)

    def test_register_handlers_with_policy(self):
        """Should update policy.KNOWN_ACTION_TYPES when policy provided."""
        execution_layer = MagicMock()
        agent = MockShugonetAgent()
        policy = MagicMock()
        policy.KNOWN_ACTION_TYPES = set()

        register_network_handlers(execution_layer, agent, policy_module=policy)

        # Policy should be updated with network types
        for action_type in NETWORK_ACTION_TYPES | NETWORK_READ_ACTION_TYPES:
            self.assertIn(action_type, policy.KNOWN_ACTION_TYPES)

    def test_register_custom_action_types(self):
        """Should allow custom action types to be registered."""
        execution_layer = MagicMock()
        agent = MockShugonetAgent()
        custom_types = ["custom_network_action"]

        register_network_handlers(
            execution_layer, agent, action_types=custom_types
        )

        # Should register only the custom type
        execution_layer.register_handler.assert_called_once()


class TestAttachNetworkFallbacks(unittest.TestCase):
    """Test fallback severity attachment."""

    def test_attach_fallbacks(self):
        """Should merge network fallback severities."""
        controller = MagicMock()
        controller.severities = {}

        attach_network_fallbacks(controller)

        # Should have network severities
        self.assertIn("network_transport_exhausted", controller.severities)
        self.assertIn("network_peer_lost", controller.severities)
        self.assertIn("memory_sync_conflict_storm", controller.severities)
        self.assertIn("audit_chain_broken", controller.severities)

    def test_attach_fallbacks_preserves_existing(self):
        """Should preserve existing severities when merging."""
        controller = MagicMock()
        controller.severities = {"existing_trigger": "pause"}

        attach_network_fallbacks(controller)

        # Existing severity should be preserved
        self.assertIn("existing_trigger", controller.severities)
        self.assertEqual(controller.severities["existing_trigger"], "pause")

    def test_attach_fallbacks_handles_error(self):
        """Should handle errors gracefully."""
        controller = MagicMock()
        controller.severities = None  # Will cause update to fail

        # Should not raise
        attach_network_fallbacks(controller)


class TestFallbackSeverities(unittest.TestCase):
    """Test that fallback severities are properly configured."""

    def test_network_fallback_severities(self):
        """Network fallback severities should be properly defined."""
        from fallbacks import DEFAULT_SEVERITIES

        self.assertEqual(
            DEFAULT_SEVERITIES["network_transport_exhausted"], "pause"
        )
        self.assertEqual(DEFAULT_SEVERITIES["network_peer_lost"], "pause")
        self.assertEqual(
            DEFAULT_SEVERITIES["memory_sync_conflict_storm"], "safe_state"
        )
        self.assertEqual(DEFAULT_SEVERITIES["audit_chain_broken"], "halt")


class TestExecutionLayerIntegration(unittest.TestCase):
    """Test integration with ExecutionLayer."""

    def test_network_types_in_allowed_handlers(self):
        """Network types should be in execution layer allowed handlers."""
        from execution_layer import ExecutionLayer

        # Create execution layer
        layer = ExecutionLayer()

        # Mock agent
        agent = MockShugonetAgent()

        # Should be able to register network handlers without error
        for action_type in NETWORK_ACTION_TYPES | NETWORK_READ_ACTION_TYPES:
            layer.register_handler(action_type, lambda d: {})


if __name__ == "__main__":
    unittest.main()
