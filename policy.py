"""
ShugoCore governance layer
==========================

- ``CapabilityRegistry``   - declarative allowlists (egress hosts, HTTP
  methods, SQL statement types, hardware commands, response caps) consulted
  by the policy gate before anything executes.
- ``ApprovalBroker``       - human-in-the-loop approval for side-effecting
  actions. Fail-closed: no operator channel attached, or TTL expiry, means
  denied.
- ``ConsentRegistry``      - external consent grants. The acting agent can
  never self-assert consent for side-effecting actions; grants come from an
  operator channel and can carry TTLs.
"""

import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from security import sanitize_text

logger = logging.getLogger(__name__)

# Actions with real-world side effects: require external consent AND approval.
SIDE_EFFECTING_ACTION_TYPES = {"api_call", "database_update", "hardware_interaction"}
# External reads: allowlisted egress + rate limiting, no consent required.
EXTERNAL_READ_ACTION_TYPES = {"news_api", "search_api"}
# Robotics actions: physical side effects, require consent AND approval.
ROBOTICS_ACTION_TYPES = {"robot_navigate", "robot_manipulate", "robot_gripper"}
# Safety-critical robotics actions: bypass consent/approval gates.
ROBOTICS_SAFETY_ACTION_TYPES = {"robot_stop"}
# Robotics read-only actions: no consent required.
ROBOTICS_READ_ACTION_TYPES = {"robot_query_state", "robot_scan"}
# Mobile fleet actions: compute offload to paired Android nodes (privacy-
# relevant - requests leave the host and run on a personal device).
MOBILE_ACTION_TYPES = {"mobile_request_compute"}
# Mobile fleet read-only actions.
MOBILE_READ_ACTION_TYPES = {"mobile_list_nodes", "mobile_node_status"}
KNOWN_ACTION_TYPES = (SIDE_EFFECTING_ACTION_TYPES | EXTERNAL_READ_ACTION_TYPES
                      | ROBOTICS_ACTION_TYPES | ROBOTICS_SAFETY_ACTION_TYPES
                      | ROBOTICS_READ_ACTION_TYPES | MOBILE_ACTION_TYPES
                      | MOBILE_READ_ACTION_TYPES | {"multi_step_process"})



# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------
class CapabilityRegistry:
    """
    Declarative action allowlists. Defaults are deliberately restrictive:
    reads against the two documented public APIs, GET-only, SELECT-only SQL
    (which has no executor anyway), and an empty hardware-command allowlist
    (all hardware actions denied until an operator configures entries).
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        config = dict(config or {})
        self.api_hosts = list(config.get("api_hosts",
                                         ["api.duckduckgo.com", "newsapi.org"]))
        methods = config.get("allowed_methods", {})
        self.allowed_methods: Dict[str, List[str]] = {
            "api_call": list(methods.get("api_call", ["GET"])),
            "news_api": list(methods.get("news_api", ["GET"])),
            "search_api": list(methods.get("search_api", ["GET"])),
        }
        self.sql_statements = list(config.get("sql_statements", ["SELECT"]))
        self.hardware_commands = set(config.get("hardware_commands", []))
        self.max_response_bytes = max(1024, int(config.get("max_response_bytes", 262144)))
        # Robotics capabilities
        self.robot_hosts = list(config.get("robot_hosts", ["localhost", "127.0.0.1"]))
        self.max_linear_velocity = max(0.01, float(config.get("max_linear_velocity", 1.0)))
        self.max_angular_velocity = max(0.01, float(config.get("max_angular_velocity", 1.0)))
        self.max_acceleration = max(0.01, float(config.get("max_acceleration", 0.5)))
        self.workspace_bounds = dict(config.get("workspace_bounds", {
            "x": (-2.0, 2.0), "y": (-2.0, 2.0), "z": (0.0, 2.0)
        }))
        self.joint_limits = dict(config.get("joint_limits", {}))
        self.max_payload = max(0.0, float(config.get("max_payload", 5.0)))
        self.watchdog_timeout = max(0.1, float(config.get("watchdog_timeout", 5.0)))
        # Mobile fleet capabilities (Android compute nodes)
        self.mobile_devices_allowlist = set(
            config.get("mobile_devices_allowlist", []))
        self.mobile_max_publish_hz = max(0.1, float(config.get("mobile_max_publish_hz", 30.0)))
        self.mobile_sensor_topics = list(config.get("mobile_sensor_topics", [
            "camera", "imu", "gps", "battery", "heartbeat",
            "compute_result", "teleop"]))
        self.mobile_compute_timeout = max(0.5, float(config.get("mobile_compute_timeout", 30.0)))
        # Loopback model endpoints permitted for on-device inference
        # (Ollama-Termux 11434, llama.cpp server 8080/8081, LM Studio 1234,
        # generic local servers 5000/8000).
        self.local_model_ports = set(
            int(p) for p in config.get("local_model_ports", [11434, 8080, 8081, 1234, 5000, 8000]))

    def validate_mobile_topic(self, device_id: str, topic_tail: str) -> Tuple[bool, str]:
        """
        Topic ACL for paired mobile nodes: they may only surface data on the
        contracted sensor namespace. Actuation topics are unreachable by
        construction.
        """
        device = str(device_id or "").strip()
        tail = str(topic_tail or "").strip("/")
        if not device or not tail:
            return False, "empty mobile topic component"
        if device not in self.mobile_devices_allowlist:
            return False, f"device '{device}' is not in the operator pairing allowlist"
        if tail not in self.mobile_sensor_topics:
            return False, (f"topic '{tail}' is outside the mobile contract "
                           f"(allowed: {self.mobile_sensor_topics})")
        return True, ""

    def validate_model_endpoint(self, url: str) -> Tuple[bool, str]:
        """
        On-device inference endpoints must be loopback HTTP(S) on an
        allowlisted port. Prevents a compromised launcher config from
        exfiltrating prompts to arbitrary hosts.
        """
        from urllib.parse import urlparse
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in ("http", "https"):
            return False, f"scheme '{parsed.scheme}' is not allowed for model endpoints"
        if parsed.username or parsed.password:
            return False, "credentials in model endpoint URLs are not allowed"
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False, (f"model endpoint host '{host}' is not loopback "
                           f"(on-device inference must stay local)")
        if parsed.port is not None and parsed.port not in self.local_model_ports:
            return False, f"port {parsed.port} is not in the local model port allowlist"
        return True, ""

    def validate_api_call(self, url: str, method: str) -> Tuple[bool, str]:
        """Scheme/host allowlist + method allowlist for generic API calls."""
        from security import validate_url  # local import avoids cycles

        ok, reason = validate_url(url, self.api_hosts)
        if not ok:
            return False, reason
        allowed = self.allowed_methods.get("api_call", ["GET"])
        if str(method).upper() not in allowed:
            return False, f"HTTP method '{method}' is not allowed (allowed: {allowed})"
        return True, ""

    def validate_sql(self, statement: str) -> Tuple[bool, str]:
        """Only single, allowlisted statement types pass (default: SELECT)."""
        cleaned = str(statement or "").strip()
        if not cleaned:
            return False, "empty SQL statement"
        if ";" in cleaned.rstrip(";"):
            return False, "multiple SQL statements are not allowed"
        first_word = cleaned.split(None, 1)[0].upper().rstrip(";")
        if first_word not in {s.upper() for s in self.sql_statements}:
            return False, (f"SQL statement type '{first_word}' is not allowed "
                           f"(allowed: {self.sql_statements})")
        return True, ""

    def validate_hardware(self, command: str) -> Tuple[bool, str]:
        """Exact-match allowlist; empty allowlist denies everything."""
        cleaned = str(command or "").strip()
        if cleaned not in self.hardware_commands:
            return False, (f"hardware command '{sanitize_text(cleaned, 80)}' is not "
                           f"in the operator-configured allowlist")
        return True, ""


# ---------------------------------------------------------------------------
# Approval broker (human-in-the-loop, fail-closed)
# ---------------------------------------------------------------------------
class ApprovalBroker:
    """
    Side-effecting actions must be approved before execution.

    Fail-closed semantics:
    - No operator channel attached  -> immediate denial.
    - Operator attached             -> request runs in a background thread;
      the caller waits up to ``ttl_seconds``; timeout means denial.
    - ``approve()`` / ``deny()``    -> programmatic operator console API.
    """

    def __init__(self, ttl_seconds: float = 30.0):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._operator: Optional[Callable[[Dict[str, Any]], bool]] = None
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def attach_operator(self, callback: Callable[[Dict[str, Any]], bool]) -> None:
        """
        Register the operator channel: ``callback(request) -> bool``. It is
        invoked in a background thread; slow humans only cost the TTL, never
        the requester beyond it.
        """
        self._operator = callback

    def request_approval(self, description: Dict[str, Any],
                         ttl_seconds: Optional[float] = None) -> Dict[str, Any]:
        """
        Ask for approval of a side-effecting action. Returns
        ``{'approved': bool, 'reason': str, 'request_id': str}``.
        """
        ttl = self.ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        with self._lock:
            if self._operator is None:
                return {"approved": False,
                        "reason": "no approval channel attached (fail-closed)",
                        "request_id": ""}
            request_id = uuid.uuid4().hex
            request = {"request_id": request_id,
                       "description": description,
                       "requested_at": time.time()}
            event = threading.Event()
            self._pending[request_id] = {"request": request, "event": event,
                                         "approved": None}

        worker = threading.Thread(target=self._ask_operator, args=(request_id,),
                                  name=f"approval-{request_id[:8]}", daemon=True)
        worker.start()

        if not event.wait(ttl):
            with self._lock:
                self._pending.pop(request_id, None)
            logger.warning(f"Approval {request_id[:8]} timed out; denying (default).")
            return {"approved": False, "reason": "approval timed out (default deny)",
                    "request_id": request_id}

        with self._lock:
            record = self._pending.pop(request_id, None)
        approved = bool(record and record.get("approved"))
        reason = "operator approved" if approved else "operator denied"
        logger.info(f"Approval {request_id[:8]}: {reason} for "
                    f"{(description or {}).get('action_type')}")
        return {"approved": approved, "reason": reason, "request_id": request_id}

    def _ask_operator(self, request_id: str) -> None:
        with self._lock:
            record = self._pending.get(request_id)
        if record is None:
            return
        try:
            result = bool(self._operator(record["request"]))  # type: ignore[misc]
        except Exception as exc:
            logger.error(f"Operator channel failed: {exc}")
            result = False
        with self._lock:
            record = self._pending.get(request_id)
            if record is not None:
                record["approved"] = result
                record["event"].set()

    def approve(self, request_id: str) -> bool:
        """Programmatic approval (operator console)."""
        return self._resolve(request_id, True)

    def deny(self, request_id: str) -> bool:
        """Programmatic denial (operator console)."""
        return self._resolve(request_id, False)

    def _resolve(self, request_id: str, approved: bool) -> bool:
        with self._lock:
            record = self._pending.get(request_id)
            if record is None or record.get("approved") is not None:
                return False
            record["approved"] = approved
            record["event"].set()
        return True

    def list_pending(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [record["request"] for record in self._pending.values()
                    if record.get("approved") is None]


# ---------------------------------------------------------------------------
# Consent registry (external grants only)
# ---------------------------------------------------------------------------
class ConsentRegistry:
    """
    Records operator-issued consent grants per action type. Grants can carry
    TTLs; expired grants stop counting immediately. There is deliberately no
    way for an executing agent to grant consent to itself.
    """

    def __init__(self):
        self._grants: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def grant(self, action_type: str, granted_by: str, scope: str = "*",
              note: str = "", ttl_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Record an external consent grant for ``action_type``."""
        entry = {
            "action_type": sanitize_text(action_type, 64),
            "granted_by": sanitize_text(granted_by, 120),
            "scope": sanitize_text(scope, 120),
            "note": sanitize_text(note, 300),
            "granted_at": time.time(),
            "expires_at": (time.time() + ttl_seconds) if ttl_seconds else None,
        }
        with self._lock:
            self._grants.setdefault(entry["action_type"], []).append(entry)
        logger.info(f"Consent granted for '{entry['action_type']}' by {entry['granted_by']}"
                    + (f" (expires in {ttl_seconds:.0f}s)" if ttl_seconds else ""))
        return entry

    def revoke(self, action_type: str) -> int:
        with self._lock:
            removed = len(self._grants.pop(str(action_type), []))
        if removed:
            logger.info(f"Consent revoked for '{action_type}' ({removed} grant(s)).")
        return removed

    def has_grant(self, action_type: str) -> bool:
        """True while at least one unexpired grant exists for the type."""
        now = time.time()
        with self._lock:
            entries = self._grants.get(str(action_type), [])
            return any(entry["expires_at"] is None or entry["expires_at"] > now
                       for entry in entries)

    def grants(self) -> Dict[str, List[Dict[str, Any]]]:
        """Read-only snapshot of all grants."""
        with self._lock:
            return {key: [dict(entry) for entry in entries]
                    for key, entries in self._grants.items()}


