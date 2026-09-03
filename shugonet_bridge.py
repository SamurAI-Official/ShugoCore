"""
ShugoCore Shogunet bridge
=========================

ShugoCore-side adapter for the Shogunet networking layer. Follows the
same pattern as robotics_handler.py and mobile_nodes.py.

This module lets a ShugoCore DecisionEngine drive the Shogunet network
stack for multi-agent collaboration over 5G, 4G, WiFi, LoRa, and Bluetooth.

Usage:
    from shugonet_bridge import (
        NETWORK_ACTION_TYPES,
        NETWORK_READ_ACTION_TYPES,
        ShugonetExecutionHandler,
        register_network_handlers,
        attach_network_fallbacks,
    )
    from agent_runtime import ShugonetAgentRuntime

    runtime = ShugonetAgentRuntime(
        agent_id="agent-001",
        host_tcp_host="127.0.0.1",
        host_tcp_port=9000,
        host_relay_url="http://127.0.0.1:9001",
    )
    runtime.connect_to_host()

    register_network_handlers(decision_engine.execution_layer, runtime)
    attach_network_fallbacks(decision_engine.fallback_controller)
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

NETWORK_ACTION_TYPES = {
    "network_send",
    "network_query",
    "network_sync",
}

NETWORK_READ_ACTION_TYPES = {
    "network_list_agents",
    "network_status",
}

NETWORK_FALLBACK_SEVERITIES = {
    "network_transport_exhausted": "pause",
    "network_peer_lost": "pause",
    "memory_sync_conflict_storm": "safe_state",
    "audit_chain_broken": "halt",
}

_NAMESPACE = "/shugunet"



class ShugonetExecutionHandler:
    """
    Execution-layer handler for Shogunet network actions.

    Mirrors the MobileExecutionHandler pattern. The ``shugonet_agent``
    parameter is the Shogunet runtime (ShugonetAgentRuntime) which must
    expose:
        - send(peer, topic, payload)
        - query(query, peers, top_k)
        - sync(peer, since)
        - list_agents()
        - status()
    """

    def __init__(self, shugonet_agent: Any):
        self.agent = shugonet_agent

    def handle(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        action_type = str(decision.get("action_type", ""))
        params = decision.get("params") or {}

        if action_type == "network_send":
            return self._send(params)
        if action_type == "network_query":
            return self._query(params)
        if action_type == "network_sync":
            return self._sync(params)
        if action_type == "network_list_agents":
            return {
                "status": "success",
                "action": "network_list_agents",
                "agents": self.agent.list_agents(),
            }
        if action_type == "network_status":
            return {
                "status": "success",
                "action": "network_status",
                **self.agent.status(),
            }
        return {
            "status": "refused",
            "reason": f"unknown network action '{action_type}'",
        }

    def _send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        peer = str(params.get("peer", ""))
        topic = str(params.get("topic", ""))
        payload = params.get("payload")
        if not peer or not topic or payload is None:
            return {"status": "refused", "reason": "peer/topic/payload required"}
        result = self.agent.send(peer, topic, payload)
        return {
            "status": result.get("status", "success"),
            "action": "network_send",
            "peer": peer,
            **result,
        }

    def _query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = params.get("query")
        if not query:
            return {"status": "refused", "reason": "query text required"}
        result = self.agent.query(
            query,
            peers=params.get("peers"),
            top_k=params.get("top_k"),
        )
        return {
            "status": "success",
            "action": "network_query",
            "results": result,
        }

    def _sync(self, params: Dict[str, Any]) -> Dict[str, Any]:
        result = self.agent.sync(
            peer=params.get("peer"),
            since=params.get("since"),
        )
        return {
            "status": "success",
            "action": "network_sync",
            **result,
        }


def network_topic(agent_id: str, tail: str) -> str:
    """Construct a canonical topic within the agent's namespace."""
    return f"{_NAMESPACE}/{agent_id}/{str(tail).strip('/')}"



def register_network_handlers(
    execution_layer: Any,
    shugonet_agent: Any,
    policy_module: Optional[Any] = None,
    action_types: Optional[List[str]] = None,
) -> None:
    """
    Register network action types and handler on a ShugoCore engine.

    Args:
        execution_layer: The engine's execution_layer (or test double)
        shugonet_agent: The Shogunet runtime instance
        policy_module: Defaults to ShugoCore's policy module
        action_types: Override the default action types to register
    """
    handler = ShugonetExecutionHandler(shugonet_agent)
    types = action_types or sorted(NETWORK_ACTION_TYPES | NETWORK_READ_ACTION_TYPES)

    policy = policy_module
    if policy is None:
        try:
            import policy as _policy
            policy = _policy
        except Exception:
            policy = None

    if policy is not None:
        if not hasattr(policy, "KNOWN_ACTION_TYPES"):
            setattr(policy, "KNOWN_ACTION_TYPES", set())
        policy.KNOWN_ACTION_TYPES.update(types)

    for action_type in types:
        execution_layer.register_handler(action_type, handler.handle)

    logger.info("registered %d shugonet action handlers", len(types))


def attach_network_fallbacks(fallback_controller: Any) -> None:
    """
    Merge Shogunet network trigger severities into a FallbackController.

    Args:
        fallback_controller: The engine's FallbackController (or duck-typed double)
    """
    try:
        fallback_controller.severities.update(NETWORK_FALLBACK_SEVERITIES)
    except Exception as exc:
        logger.warning("network fallback severities not merged: %s", exc)
