"""
ShugoCore Android node runtime
==============================

Entry point for running ShugoCore ON an Android device as a first-class
compute node in the ROS 2 graph (see docs/android_integration.md).

Roles (``--role``):

- ``sensor_node``    - publishes phone sensors (camera/imu/gps/battery) and
  heartbeats into the mobile contract namespace.
- ``compute_node``   - accepts compute requests (on-device ML via the Kotlin
  shell's LiteRT/NNAPI stack) and returns results.
- ``operator_node``  - relays teleop intent to a HOST-relay topic. Teleop
  commands NEVER go directly to robot actuation topics; the host engine
  gates them like any other action.
- ``full_agent``     - the complete ShugoCore engine (governor, fallbacks,
  memory tiers, gated execution) driven by an OFFLINE local model: an
  Ollama-compatible or OpenAI-compatible launcher on loopback (Ollama on
  Termux, llama.cpp ``llama-server``, LM Studio). Endpoint validation
  (loopback-only, port allowlist) is enforced by ``CapabilityRegistry``.

Transport selection: a Kotlin bridge object (Chaquopy) uses the in-process
jros2/Fast-DDS path; a rosbridge host uses the WebSocket path; otherwise a
deterministic stub enables fully offline development.

Run (Termux)::

    python3 android_node.py --role sensor_node --device-id pixel8 --domain 42
"""

import argparse
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from acceleration import AccelerationPolicy, detect_platform
from android_bridge import JavaBridgeROS2Interface, RosBridgeInterface
from android_runtime import AndroidRuntime
from audit import AuditChain
from memory_system import MemoryManager
from policy import CapabilityRegistry
from ros2_interface import StubROS2Interface, Twist, sanitize_twist
from security import sanitize_text
from state_machine import ExecutionGovernor
from fallbacks import FallbackController
from telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer("shugocore.android_node")

ROLES = ("sensor_node", "compute_node", "operator_node", "full_agent")

# Loopback launchers probed for on-device offline inference.
LAUNCHER_PROBES: List[Dict[str, Any]] = [
    {"launcher": "ollama", "base_url": "http://127.0.0.1:11434",
     "api": "ollama", "probe_path": "/api/tags"},
    {"launcher": "llama.cpp", "base_url": "http://127.0.0.1:8080",
     "api": "openai", "probe_path": "/health"},
    {"launcher": "llama.cpp", "base_url": "http://127.0.0.1:8081",
     "api": "openai", "probe_path": "/health"},
    {"launcher": "lmstudio", "base_url": "http://127.0.0.1:1234",
     "api": "openai", "probe_path": "/v1/models"},
]


def detect_local_launcher(prober: Optional[Callable[[str], bool]] = None,
                          candidates: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """
    Probe loopback ports for a local inference launcher. Returns the first
    responder as ``{"launcher", "base_url", "api"}`` or None. ``prober`` is
    injectable for tests (default: HTTP GET with a 0.5s timeout).
    """
    prober = prober or (lambda url: _probe_url(url))
    for candidate in (candidates or LAUNCHER_PROBES):
        url = f"{candidate['base_url']}{candidate['probe_path']}"
        try:
            if prober(url):
                return {"launcher": candidate["launcher"],
                        "base_url": candidate["base_url"],
                        "api": candidate["api"]}
        except Exception:
            continue
    return None


def _probe_url(url: str) -> bool:
    try:
        response = requests.get(url, timeout=0.5)
        return response.status_code < 500
    except Exception:
        return False


def mobile_topic(device_id: str, tail: str) -> str:
    """Canonical mobile contract topic: /shugocore/mobile/{device_id}/{tail}."""
    return f"/shugocore/mobile/{sanitize_text(device_id, 48)}/{str(tail).strip('/')}"


class NodeConfig:
    """Configuration for an Android node instance."""

    def __init__(self,
                 device_id: str,
                 role: str = "sensor_node",
                 domain_id: int = 0,
                 bridge: Optional[Any] = None,
                 rosbridge_host: Optional[str] = None,
                 rosbridge_port: int = 9090,
                 sensors: Optional[List[str]] = None,
                 publish_hz: float = 10.0,
                 capabilities: Optional[CapabilityRegistry] = None,
                 db_path: str = "node_semantic_memory.db",
                 audit_path: str = "node_audit.jsonl"):
        if role not in ROLES:
            raise ValueError(f"unknown role '{role}' (available: {ROLES})")
        self.device_id = sanitize_text(device_id, 48)
        if not self.device_id:
            raise ValueError("device_id is required")
        self.role = role
        self.domain_id = max(0, min(232, int(domain_id)))
        self.bridge = bridge
        self.rosbridge_host = rosbridge_host
        self.rosbridge_port = max(1, min(65535, int(rosbridge_port)))
        self.sensors = [sanitize_text(s, 32) for s in (sensors or ["battery", "imu", "gps"])]
        self.publish_hz = max(0.5, min(50.0, float(publish_hz)))
        self.capabilities = capabilities if capabilities is not None else CapabilityRegistry()
        self.db_path = str(db_path)
        self.audit_path = str(audit_path)

class AndroidShugoCoreNode:
    """
    One ShugoCore node on an Android device. Assembles governance (governor,
    fallback controller, memory tiers, audit chain, acceleration policy),
    selects the ROS 2 transport, and runs the configured role loop.
    """

    def __init__(self, config: NodeConfig):
        self.config = config
        self.device_id = config.device_id
        self.audit = AuditChain(config.audit_path)
        self.acceleration = AccelerationPolicy(audit=self.audit)
        self.governor = ExecutionGovernor()
        self.fallbacks = FallbackController(governor=self.governor, audit=self.audit)
        self.memory = MemoryManager(agent_id=f"android-{self.device_id}",
                                    semantic=None, core=None,
                                    episodic_journal_path=f"node_journal_{self.device_id}.jsonl")
        self.runtime = AndroidRuntime(
            bridge=config.bridge,
            fallbacks=self.fallbacks,
            acceleration=self.acceleration)
        self.interface = self._select_transport()
        self.engine: Optional[Any] = None  # full_agent only
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._hb_seq = 0

    def _select_transport(self):
        cfg = self.config
        if cfg.bridge is not None:
            return JavaBridgeROS2Interface(cfg.bridge, domain_id=cfg.domain_id,
                                           rate_limit_hz=cfg.publish_hz)
        if cfg.rosbridge_host:
            return RosBridgeInterface(host=cfg.rosbridge_host, port=cfg.rosbridge_port,
                                      rate_limit_hz=cfg.publish_hz)
        logger.info("No bridge/rosbridge configured; using stub transport (offline dev)")
        return StubROS2Interface(rate_limit_hz=cfg.publish_hz)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._worker is not None:
            return
        self.runtime.on_create()
        # Heartbeat publisher is shared by every role loop.
        self.interface.create_publisher(
            mobile_topic(self.device_id, "heartbeat"), "std")
        self._audit("node_started", {"role": self.config.role,
                                     "device": self.device_id})
        if self.config.role == "full_agent":
            self._assemble_full_agent()
        target = {
            "sensor_node": self._sensor_loop,
            "compute_node": self._compute_loop,
            "operator_node": self._operator_loop,
            "full_agent": self._agent_loop,
        }[self.config.role]
        self._stop.clear()
        self._worker = threading.Thread(target=target,
                                        name=f"shugocore-{self.config.role}",
                                        daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=3.0)
            self._worker = None
        try:
            self.interface.shutdown()
        except Exception:
            pass
        try:
            self.memory.shutdown()
        except Exception:
            pass
        self._audit("node_stopped", {"device": self.device_id})

    # -- shared loops -----------------------------------------------------------
    def _publish_heartbeat(self) -> None:
        self._hb_seq += 1
        try:
            self.interface.publish(
                mobile_topic(self.device_id, "heartbeat"),
                {"device_id": self.device_id, "role": self.config.role,
                 "seq": self._hb_seq, "ts": time.time()})
        except Exception as exc:
            logger.debug("Heartbeat publish failed: %s", exc)

    def _sensor_loop(self) -> None:
        period = 1.0 / self.config.publish_hz
        for sensor in self.config.sensors:
            self.interface.create_publisher(
                mobile_topic(self.device_id, sensor), "std")
        next_beat = time.monotonic()
        while not self._stop.is_set():
            for sensor in self.config.sensors:
                reading = self._read_sensor(sensor)
                if reading is not None:
                    try:
                        self.interface.publish(
                            mobile_topic(self.device_id, sensor), reading)
                    except Exception as exc:
                        logger.debug("Sensor publish failed: %s", exc)
            if time.monotonic() >= next_beat:
                self._publish_heartbeat()
                next_beat = time.monotonic() + 2.0
            self._stop.wait(period)

    def _read_sensor(self, sensor: str) -> Optional[Dict[str, Any]]:
        """Sensor readings come from the bridge (Android APIs); stub returns None."""
        fn = getattr(self.config.bridge, "readSensor", None)
        if fn is None:
            return None
        try:
            raw = fn(sensor)
            return raw if isinstance(raw, dict) else None
        except Exception as exc:
            logger.debug("readSensor(%s) failed: %s", sensor, exc)
            return None

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass

    # -- role loops ------------------------------------------------------------
    def _compute_loop(self) -> None:
        """Accept compute requests; execute via the Kotlin shell's on-device
        ML stack (LiteRT + NPU/GPU delegates); publish results."""
        request_topic = mobile_topic(self.device_id, "compute_request")
        result_topic = mobile_topic(self.device_id, "compute_result")
        self.interface.create_publisher(result_topic, "std")
        queue: List[Dict[str, Any]] = []
        self.interface.create_subscriber(request_topic, "std",
                                         lambda msg: queue.append(msg)
                                         if isinstance(msg, dict) else None)
        accelerator = self.acceleration.preferred("vision")
        while not self._stop.is_set():
            self.interface.spin_once(timeout=0.1)
            self._publish_heartbeat()
            while queue:
                request = queue.pop(0)
                self._run_compute_request(request, result_topic, accelerator)
            self._stop.wait(0.05)

    def _run_compute_request(self, request: Dict[str, Any], result_topic: str,
                             accelerator: Any) -> None:
        request_id = sanitize_text(str(request.get("request_id", "")), 64)
        workload = sanitize_text(str(request.get("workload", "vision")), 32)
        with tracer.start_span("android.compute", {
                "workload": workload,
                "accelerator": accelerator.kind.value if accelerator else "cpu"}) as span:
            runner = getattr(self.config.bridge, "runInference", None)
            if runner is None:
                result = {"error": "no inference runtime on bridge"}
            else:
                try:
                    raw = runner(workload, json.dumps(request.get("payload", {})))
                    result = raw if isinstance(raw, dict) else {"result": raw}
                except Exception as exc:
                    logger.warning("Inference failed: %s", exc)
                    result = {"error": str(exc)[:150]}
            span.set_attribute("status", "error" if "error" in result else "ok")
        payload = {"request_id": request_id, "device_id": self.device_id,
                   "accelerator": accelerator.kind.value if accelerator else "cpu",
                   "ts": time.time(), **result}
        try:
            self.interface.publish(result_topic, payload)
        except Exception as exc:
            logger.warning("Result publish failed: %s", exc)
        self._audit("compute_executed", {"request_id": request_id,
                                         "workload": workload})

    def _operator_loop(self) -> None:
        """
        Relay teleop intent to the HOST relay topic. The host engine gates
        it through the full policy stack before anything reaches a robot -
        phones never write actuation topics directly.
        """
        teleop_topic = mobile_topic(self.device_id, "teleop")
        relay_topic = "/shugocore/teleop_relay"
        self.interface.create_publisher(relay_topic, "Twist")
        relayed: List[Dict[str, Any]] = []

        def _accept(msg: Any) -> None:
            """Teleop payloads cross the wire as raw dicts; the interface
            reconstructs linear+angular payloads as Twist objects. Accept both."""
            if isinstance(msg, dict):
                relayed.append(msg)
            elif isinstance(msg, Twist):
                relayed.append({
                    "linear": msg.linear.to_dict(),
                    "angular": msg.angular.to_dict()})

        self.interface.create_subscriber(teleop_topic, "std", _accept)
        while not self._stop.is_set():
            self.interface.spin_once(timeout=0.1)
            self._publish_heartbeat()
            while relayed:
                raw = relayed.pop(0)
                twist = self._twist_from_teleop(raw)
                try:
                    self.interface.publish(relay_topic, twist)
                except Exception as exc:
                    logger.debug("Teleop relay failed: %s", exc)
            self._stop.wait(0.05)

    def _twist_from_teleop(self, raw: Dict[str, Any]) -> Twist:
        """Clamp phone teleop input to the same velocity limits as the engine."""
        linear = raw.get("linear") if isinstance(raw.get("linear"), dict) else {}
        angular = raw.get("angular") if isinstance(raw.get("angular"), dict) else {}

        def _f(d: Dict[str, Any], key: str) -> float:
            try:
                return float(d.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0

        caps = self.config.capabilities
        twist = Twist()
        twist.linear.x = max(-caps.max_linear_velocity,
                             min(caps.max_linear_velocity, _f(linear, "x")))
        twist.linear.y = max(-caps.max_linear_velocity,
                             min(caps.max_linear_velocity, _f(linear, "y")))
        twist.linear.z = 0.0
        twist.angular.z = max(-caps.max_angular_velocity,
                              min(caps.max_angular_velocity, _f(angular, "z")))
        return sanitize_twist(twist)

    # -- full_agent ---------------------------------------------------------------
    def _assemble_full_agent(self) -> None:
        """Build the complete engine on an offline local model."""
        from decision_engine import DecisionEngine  # deferred: heavy import

        launcher = detect_local_launcher()
        endpoint_ok = bool(launcher) and self.config.capabilities.validate_model_endpoint(
            launcher["base_url"])[0]
        if launcher and endpoint_ok:
            backend_cfg = ({"type": "ollama", "base_url": launcher["base_url"]}
                           if launcher["api"] == "ollama"
                           else {"type": "openai", "base_url": launcher["base_url"],
                                 "api_key_env": "SHUGOCORE_LOCAL_KEY"})
            models = [{"id": "local", "name": "local", "backend": backend_cfg["type"],
                       "backend_config": backend_cfg}]
            self._audit("local_launcher_detected", launcher)
        else:
            models = [{"id": "stub", "name": "stub", "backend": "stub"}]
            self._audit("local_launcher_unavailable", {"fallback": "stub"})
        self.engine = DecisionEngine(
            models=models,
            vector_db_config={"type": "chroma",
                             "collection_name": f"node_{self.device_id}"},
            capabilities=self.config.capabilities,
            audit_path=self.config.audit_path,
            episodic_journal_path=f"node_journal_{self.device_id}.jsonl")

    def run_autonomous_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """App-shell entry point: execute one task through the gated engine."""
        if self.engine is None:
            return {"status": "error", "reason": "full_agent not assembled"}
        return self.engine.execute_task(task)

    def _agent_loop(self) -> None:
        """Keepalive loop for full_agent: heartbeats + inbound drain. Task
        execution is driven by the app shell via run_autonomous_task()."""
        while not self._stop.is_set():
            self._publish_heartbeat()
            try:
                self.interface.spin_once(timeout=0.2)
            except Exception:
                pass
            self._stop.wait(1.0)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ShugoCore Android node")
    parser.add_argument("--role", default="sensor_node", choices=ROLES)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--domain", type=int, default=0)
    parser.add_argument("--hz", type=float, default=10.0)
    parser.add_argument("--sensors", default="battery,imu,gps")
    parser.add_argument("--rosbridge-host", default=None,
                        help="use rosbridge instead of the in-process bridge")
    parser.add_argument("--rosbridge-port", type=int, default=9090)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = NodeConfig(device_id=args.device_id, role=args.role,
                        domain_id=args.domain, publish_hz=args.hz,
                        rosbridge_host=args.rosbridge_host,
                        rosbridge_port=args.rosbridge_port,
                        sensors=[s for s in args.sensors.split(",") if s])
    node = AndroidShugoCoreNode(config)
    node.start()
    logger.info("Node '%s' running (role=%s, platform=%s). Ctrl-C to stop.",
                args.device_id, args.role, detect_platform())
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        if node.engine is not None:
            try:
                node.engine.shutdown()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




