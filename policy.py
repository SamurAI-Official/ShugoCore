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
# Internal, non-side-effecting actions: always safe, no consent or approval
# required (they write only to the agent's own memory).
OBSERVATION_ACTION_TYPES = {"record_observation"}
# Speech output: the agent addressing the LOCAL human through the device's
# own speaker (TTS). Speaking to the operator is not egress — nothing leaves
# the host — and the text is sanitized like any memory write, so it rides in
# the internal class: no consent, no approval, but always journaled.
SPEECH_OUTPUT_ACTION_TYPES = {"speak"}
# v1.16: the agent ASKING the human a question (uncertainty -> ask). Speech
# output to the local operator again — internal class, no consent, no
# approval, always journaled. Asking never unlocks anything by itself: a
# spoken "yes" is DATA the agent may reason over, never a consent record
# (consent stays with the operator's explicit registry entry).
ASK_USER_ACTION_TYPES = {"ask_user"}
# Robotics actions: physical side effects, require consent AND approval.
ROBOTICS_ACTION_TYPES = {"robot_navigate", "robot_manipulate", "robot_gripper", "robot_look"}
# Safety-critical robotics actions: bypass consent/approval gates.
ROBOTICS_SAFETY_ACTION_TYPES = {"robot_stop"}
# Robotics read-only actions: no consent required.
ROBOTICS_READ_ACTION_TYPES = {"robot_query_state", "robot_scan"}
# Mobile fleet actions: compute offload to paired Android nodes (privacy-
# relevant - requests leave the host and run on a personal device).
MOBILE_ACTION_TYPES = {"mobile_request_compute"}
# Mobile fleet read-only actions.
MOBILE_READ_ACTION_TYPES = {"mobile_list_nodes", "mobile_node_status"}
# Network/Shogunet actions: multi-agent collaboration over 5G/4G/WiFi/LoRa/BT.
# A bridge may extend these *in place* at registration (shugonet_bridge merges its
# 1.30 spatial/NRR seam this way, because the execution layer holds a reference to
# this object and would otherwise validate a stale set), so this set grows during
# a process. Assert membership, never an exact snapshot.
NETWORK_ACTION_TYPES = {"network_send", "network_query", "network_sync"}
# Network read-only actions: no consent required.
NETWORK_READ_ACTION_TYPES = {"network_list_agents", "network_status"}
# Fleet deployment actions: rolling a signed build onto operator-allowlisted,
# ADB-reachable devices. Side-effecting (it changes what software runs on
# another machine) so the engine consent-gates it exactly like the
# SIDE_EFFECTING class, and the handler adds its own target allowlist,
# artifact-root containment and hash check on top. Host-only: the module is
# deliberately not in the Android bundle, so a device can never propose it.
# ``fleet_dev_task`` rides in the same class for the same reason -- it runs a
# named task on another node, which changes that machine -- and it adds a
# stricter rule of its own: the wire carries a task *name*, never an argv
# (dev_tasks.NAMED_TASKS is the peer's own registry of what a name means).
FLEET_ACTION_TYPES = {"fleet_deploy", "fleet_dev_task"}
# Fleet deployment read-only actions: which devices are attached, and which
# build each one is running. No consent required.
FLEET_READ_ACTION_TYPES = {"fleet_status"}
KNOWN_ACTION_TYPES = (SIDE_EFFECTING_ACTION_TYPES | EXTERNAL_READ_ACTION_TYPES
                      | ROBOTICS_ACTION_TYPES | ROBOTICS_SAFETY_ACTION_TYPES
                      | ROBOTICS_READ_ACTION_TYPES | MOBILE_ACTION_TYPES
                      | MOBILE_READ_ACTION_TYPES | NETWORK_ACTION_TYPES
                      | NETWORK_READ_ACTION_TYPES | OBSERVATION_ACTION_TYPES
                      | SPEECH_OUTPUT_ACTION_TYPES | ASK_USER_ACTION_TYPES
                      | FLEET_ACTION_TYPES | FLEET_READ_ACTION_TYPES
                      | {"multi_step_process"})



# Acoustic perception (v1.30.24). The microphone is a resource with exactly ONE owner:
# Android gives audio to a single capture at a time, so "may this node listen for sound?"
# is a consent question rather than a runtime race between providers. States and modes
# mirror sound/schema.py; they are repeated here deliberately, so the policy layer keeps no
# import dependency on the contract module.
MICROPHONE_STATES = ("available", "busy_speech", "denied", "absent")
SOUND_MODES = ("sound", "speech", "off")
# The action type an operator grants to allow *sound* perception (not speech recognition,
# which is already covered by the platform's own RECORD_AUDIO permission).
MIC_CONSENT_ACTION = "listen_microphone"


def model_posture_decision(*, hive_url: str = "", node_role: str = "primary-capable",
                           local_model: bool = False, hive_peers: int = 0) -> Tuple[bool, str]:
    """Whether THIS node may load a model of its own, and why. Returns (allowed, reason).

    A desktop hive holds the model. A phone in that hive provides sensors and memory, and a
    second copy of the weights is exactly what this posture exists to avoid -- it is memory the
    device does not have (the mobile posture is written for a phone with ~100 MB free), and it
    is generation capacity the hive already owns.

    An earlier version of this comment claimed the resident model costs more than a full core.
    Measurement says otherwise, so the record is corrected rather than left standing: idle CPU
    was indistinguishable with the model loaded (102.8 mean) versus without it (104.0), and
    105.5 with *everything* off -- a loaded GGUF is memory, not CPU, and it costs when it
    generates. What those same runs did measure is the acoustic perception layer's own cost,
    +24 points of one core (126.4 and 131.9 listening against 102.8 speech, spreads
    non-overlapping), which is a separate argument for keeping the microphone's work honest.
    """
    if not local_model:
        return False, "configured without a local model"
    hive = str(hive_url or "").strip()
    if hive:
        # The default api_url is loopback, not a hive: "127.0.0.1" means "no desktop here", and
        # treating a non-empty URL as a hive would strip the model from every standalone node.
        host = hive.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].strip().lower()
        if host not in ("", "127.0.0.1", "localhost", "::1"):
            return False, "a desktop hive is configured: the hive holds the model"
    if str(node_role or "").strip().lower() == "follower":
        return False, "follower posture: this node provides sensors and memory, not inference"
    if int(hive_peers or 0) > 0:
        return False, "a hive peer is live on the mesh: the hive holds the model"
    return True, "standalone node with a local model configured"


def sound_listen_decision(capabilities: Any = None, consent: Any = None,
                          node_role: str = "primary-capable") -> Tuple[str, str]:
    """Which layer may hold the microphone on this node, and why. Returns (mode, reason).

    The microphone is the one sensor whose misuse an operator cannot see for themselves: a node
    that listens and says nothing looks exactly like a node that is switched off. So the
    precedence is explicit, and "what this node is" is part of it:

    1. ``off`` -- acoustic perception is switched off here. Restrictive, so it needs nothing
       else to justify itself.
    2. Not permitted (``sound_enabled`` false) -> ``speech``: the behaviour the fleet has.
    3. No external consent grant -> ``speech``. The agent cannot issue this to itself.
    4. An explicitly pinned mode -> that mode. A per-node operator choice beats the posture.
    5. Otherwise the posture decides. A **follower** is a sensor provider for a hive that holds
       the model, so acoustic sensing *is* its microphone duty -> ``sound``. A standalone or
       primary-capable node speaks with the person in the room -> ``speech``.

    The returned reason names the rule that applied, because a silent node has to be able to say
    why it is silent -- and an override has to be visible *as* an override.
    """
    mode = str(getattr(capabilities, "sound_listen_mode", "sound") or "sound").lower()
    if mode not in SOUND_MODES:
        return "speech", f"unknown configured sound mode {mode!r}"
    if mode == "off":
        return "off", "operator configured acoustic perception off"
    if capabilities is None or not bool(getattr(capabilities, "sound_enabled", False)):
        return "speech", "sound perception is not enabled in configuration for this node"
    if consent is None:
        return "speech", f"no consent registry, so no grant for '{MIC_CONSENT_ACTION}'"
    try:
        granted = bool(consent.has_grant(MIC_CONSENT_ACTION))
    except Exception:
        granted = False
    if not granted:
        return "speech", f"no external consent grant for '{MIC_CONSENT_ACTION}'"
    if bool(getattr(capabilities, "sound_mode_pinned", False)):
        return mode, f"operator pinned listen_mode={mode} for this node"
    if str(node_role or "").strip().lower() == "follower":
        return "sound", ("follower posture (sensor provider): acoustic sensor duty, "
                         "consent on record")
    return "speech", "standalone posture: the microphone stays with speech recognition"


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
            "camera", "imu", "gps", "battery", "heartbeat", "microphone",
            "compute_result", "teleop"]))
        self.mobile_compute_timeout = max(0.5, float(config.get("mobile_compute_timeout", 30.0)))
        # Loopback model endpoints permitted for on-device inference
        # (Ollama-Termux 11434, llama.cpp server 8080/8081, LM Studio 1234,
        # generic local servers 5000/8000).
        self.local_model_ports = set(
            int(p) for p in config.get("local_model_ports", [11434, 8080, 8081, 1234, 5000, 8000]))
        # Acoustic perception (v1.30.24). See sound_listen_decision(): the microphone has one
        # owner, and sound classification stays off unless an operator enables it here *and*
        # records an external consent grant for MIC_CONSENT_ACTION.
        self.sound_enabled = bool(config.get("sound_enabled", False))
        self.sound_listen_mode = str(config.get("sound_listen_mode", "sound")).lower()
        if self.sound_listen_mode not in SOUND_MODES:
            self.sound_listen_mode = "sound"
        # True when an operator named a mode for THIS node ("sound"/"speech"/"off"), as opposed
        # to the mode merely being a default. A pin beats the posture; without one, a follower
        # falls to its sensor duty and a standalone node keeps speech recognition.
        self.sound_mode_pinned = bool(config.get("sound_mode_pinned", False))
        self.sound_max_windows_per_minute = max(
            1, int(config.get("sound_max_windows_per_minute", 60)))

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
            # A programmatic approve()/deny() may have resolved the request
            # while the human channel was still thinking (v1.30.4 CSFA fix).
            # The human's late verdict must NOT overwrite an already-issued
            # resolution — first resolution wins (fail-closed either way).
            if record is not None and record.get("approved") is None:
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

    def has_grant_for_attended_action(self, action_type: str,
                                       attention_state: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """v1.20: consent check augmented with attention verification.

        Returns (allowed, reason). Consent-gated actions (speak, robot
        motion, etc.) are only allowed when:
          - At least one unexpired grant exists for the type, AND
          - The attention state is ATTENDING or UNKNOWN (fail-open for
            uncertainty; fail-closed for ABSENT/DIVERTED).

        When attention_state is None (no attention layer wired), falls
        back to the standard has_grant check (unchanged behavior).
        """
        if attention_state is None:
            return self.has_grant(action_type), None
        if attention_state not in ("attending", "unknown"):
            return False, f"human attention is '{attention_state}' (need 'attending' or 'unknown')"
        if not self.has_grant(action_type):
            return False, f"no external consent grant for '{action_type}'"
        return True, None

    def grants(self) -> Dict[str, List[Dict[str, Any]]]:
        """Read-only snapshot of all grants."""
        with self._lock:
            return {key: [dict(entry) for entry in entries]
                    for key, entries in self._grants.items()}


