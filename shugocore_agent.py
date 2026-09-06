# Android agent entrypoint (Chaquopy). Called from ShugoCoreService.
import logging
import os
import threading
import time
import traceback
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("shugocore_android")

# The orchestration loop stages surfaced on the AGENT tab. Stage tracking is
# honest: a stage is only reported as run when the code path for it actually
# executed during the tick.
PIPELINE_STAGES = ("OBSERVE", "GATE", "DECIDE",
                   "EXECUTE", "EVALUATE", "RECORD", "CONSOLIDATE")

_LOG_BUFFER_MAX = 300


class AndroidAgent:
    def __init__(self, device_caps: Optional[str] = None,
                 api_url: Optional[str] = None,
                 data_dir: Optional[str] = None):
        self.device_caps = device_caps or "Unknown"
        self.api_url = api_url or "http://127.0.0.1:11434"
        # Writable app-private dir injected by the Kotlin shell. Android apps
        # cannot rely on the process cwd (root "/" is read-only) — without
        # this, MemoryManager/DecisionEngine fail to open their SQLite files
        # and the whole agent construction throws.
        self.data_dir = data_dir
        self.memory_db_path = self._join_data("semantic_memory.db")
        self.audit_path = self._join_data("audit_chain.jsonl")
        self.episodic_journal_path = self._join_data("episodic_journal.jsonl")
        self.tick_count = 0
        # Phone telemetry pushed from the Kotlin ThermalMonitor each tick.
        self.telemetry: Dict[str, Any] = {}
        self.last_observation: Dict[str, Any] = {}
        # Capability declarations pushed by the Kotlin SensorCapabilityManager.
        # Android permission != agent authority: this registry only records
        # what the hardware layer declared; authority lives in self.agent_caps.
        self.capabilities: Dict[str, Dict[str, Any]] = {}
        # SECURITY-tab authority toggles + network policy (enforced in tick()).
        self.agent_caps: Dict[str, bool] = {}
        self.network_policy: Dict[str, bool] = {"internet": False, "lan": True,
                                                "localhost": True}
        # Bounded seq-numbered log buffer the Kotlin UI polls (LogBus source).
        self._log_seq = 0
        self._log_buffer: deque = deque(maxlen=_LOG_BUFFER_MAX)
        self._log_lock = threading.Lock()
        # Last-tick pipeline / outcome state for the AGENT tab.
        self._last_stages: List[str] = []
        self._last_decision = "—"
        self._last_action = "—"
        self._last_evaluation = "—"
        self._last_tick_ts = 0.0
        # Any construction failures, captured so the UI can surface the real
        # reason instead of a silent zombie agent (cycle=0 forever).
        self.engine_error: Optional[str] = None
        self.init_error: Optional[str] = None
        self.memory = None
        self.capability_registry = None
        self.consent_registry = None
        self.engine: Optional[Any] = None
        self._bootstrap()
        self.log("AGENT", f"agent ready (device={self.device_caps}, "
                          f"api={self.api_url}, "
                          f"engine={self.engine.__class__.__name__ if self.engine else 'None'})")

    def _join_data(self, name: str) -> Optional[str]:
        """Resolve a state file relative to the writable data dir."""
        if not self.data_dir:
            return None
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            return str(Path(self.data_dir) / name)
        except Exception:
            return None

    def _bootstrap(self) -> None:
        """Robustly construct the agent. NEVER raises: a failure here must
        degrade to a reporting agent, not a null pyAgent (which kills the
        whole tick loop and shows a zombie 'RUNNING' in the UI).

        The Chaquopy process cwd is "/" (read-only on Android). All bundled
        modules that open files with relative paths (LoggingManager →
        ``decision_engine.log``, SemanticMemory, audit journals) would hit
        ``OSError: [Errno 30] Read-only file system`` unless we chdir into the
        app-private writable dir first. ``data_dir`` is injected from Kotlin
        (``filesDir.absolutePath``) and is the canonical writable location.
        """
        prev_cwd: Optional[str] = None
        if self.data_dir:
            try:
                prev_cwd = os.getcwd()
                os.chdir(self.data_dir)
                print(f"[SHUGOCORE_BOOT] chdir ok: {self.data_dir} (was {prev_cwd})", flush=True)
                self.log("AGENT", f"cwd -> {self.data_dir}")
            except Exception as exc:
                print(f"[SHUGOCORE_BOOT] chdir FAILED: {exc}", flush=True)
                self.log("ERROR", f"chdir failed: {exc}", level="ERROR")
        else:
            print(f"[SHUGOCORE_BOOT] WARNING: data_dir is None/empty, cwd stays {os.getcwd()}", flush=True)
        try:
            try:
                from model_backends import create_backend  # noqa: F401
                import android_inference  # noqa: F401  (registers "android" backend)
            except Exception as exc:
                self.init_error = f"backend imports: {exc}"
                self.log("ERROR", f"backend imports failed: {exc}", level="ERROR")
            try:
                from memory_system import MemoryManager, SemanticMemory
                semantic = (SemanticMemory(db_path=self.memory_db_path)
                            if self.memory_db_path else None)
                self.memory = MemoryManager(agent_id=f"android-{self.device_caps}",
                                            auto_start=False,
                                            semantic=semantic)
            except Exception as exc:
                self.init_error = f"memory init: {exc}"
                self.log("ERROR", f"memory init failed: {exc}", level="ERROR")
            try:
                from policy import CapabilityRegistry, ConsentRegistry
                self.capability_registry = CapabilityRegistry()
                self.consent_registry = ConsentRegistry()
            except Exception as exc:
                self.init_error = f"policy surface: {exc}"
                self.log("ERROR", f"policy surface failed: {exc}", level="ERROR")
            self.engine = self._initialize_engine()
            if self.engine is None and not self.engine_error:
                self.engine_error = "engine initialization returned None"
            self._write_diagnostics()
            if self.init_error:
                self.log("ERROR", f"agent degraded: {self.init_error}", level="ERROR")
        finally:
            if prev_cwd is not None:
                try:
                    os.chdir(prev_cwd)
                except Exception:
                    pass

    def _write_diagnostics(self) -> None:
        """Best-effort: persist any failure traces under the data dir."""
        try:
            if not self.data_dir:
                return
            if self.engine_error:
                with open(str(Path(self.data_dir) / "engine_error.txt"), "w") as fh:
                    fh.write(self.engine_error)
            if self.init_error:
                with open(str(Path(self.data_dir) / "agent_error.txt"), "w") as fh:
                    fh.write(self.init_error)
        except Exception:
            pass

    def _initialize_engine(self) -> Optional[Any]:
        try:
            # Lazy import: the full engine stack is optional on-device. If
            # decision_engine (or any transitive module) is not bundled or
            # its deps fail, the agent degrades to the stub observation loop
            # (telemetry/observations still flow) instead of crashing
            # agent construction.
            from decision_engine import DecisionEngine
            # `api_url` matches AndroidBackend.__init__'s signature.
            kwargs: Dict[str, Any] = {
                "models": [{"id": "shugocore-local", "type": "text", "weight": 1.0,
                            "backend": {"type": "android", "api_url": self.api_url,
                                        "model_name": "shugocore-local",
                                        "device_caps": {"soc": self.device_caps}}}],
                "vector_db_config": {"type": "chroma"},
                "memory_db_path": self.memory_db_path or "semantic_memory.db",
            }
            if self.audit_path:
                kwargs["audit_path"] = self.audit_path
            if self.episodic_journal_path:
                kwargs["episodic_journal_path"] = self.episodic_journal_path
            # On-device: the Python cwd is "/" (read-only). Direct file-backed
            # artifacts (decision_engine.log, audit_chain.jsonl) at the writable
            # data dir so logging / audit don't fail with EROFS.
            if self.data_dir:
                kwargs["log_dir"] = self.data_dir
            return DecisionEngine(**kwargs)
        except Exception as exc:
            logger.error("Failed to initialize engine: %s", exc, exc_info=True)
            self.engine_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            self.log("ERROR", f"DecisionEngine init failed: {self.engine_error.splitlines()[0]}",
                     level="ERROR")
            return None

    # -- control-plane surface (called from Kotlin via Chaquopy) --------------

    def update_capabilities_json(self, caps_json: str) -> Dict[str, Any]:
        """JSON-string form of update_capabilities. Kotlin Lists arrive as
        java.util.ArrayList proxies that Chaquopy cannot iterate, so the
        declarations cross the boundary as JSON instead."""
        import json as _json
        try:
            return self.update_capabilities(_json.loads(caps_json or "[]"))
        except Exception as exc:
            self.log("SENSOR", f"capability declaration rejected: {exc}", level="ERROR")
            return {"acked": [], "error": str(exc)}

    def log(self, category: str, message: str, level: str = "INFO") -> None:
        """Append to the bounded log buffer the Android LOG tab polls."""
        with self._log_lock:
            self._log_seq += 1
            self._log_buffer.append({
                "seq": self._log_seq, "ts": time.time(),
                "level": str(level), "category": str(category).upper(),
                "message": str(message)[:220],
            })

    def recent_logs(self, after_seq: int = 0) -> List[Dict[str, Any]]:
        """Log entries with seq > after_seq (oldest first)."""
        with self._log_lock:
            return [dict(e) for e in self._log_buffer if e["seq"] > after_seq]

    def recent_logs_json(self, after_seq: int = 0) -> str:
        """JSON array of log entries — the Android LOG tab's preferred form
        because it sidesteps Chaquopy's brittle nested-dict container
        conversion (which otherwise throws 'Cannot convert dict to Map')."""
        import json as _json
        with self._log_lock:
            entries = [dict(e) for e in self._log_buffer if e["seq"] > after_seq]
            # round-trip primitive values so the JSON is unconditionally valid
            clean = []
            for e in entries:
                clean.append({
                    "seq": int(e["seq"]),
                    "ts": float(e["ts"]),
                    "level": str(e["level"]),
                    "category": str(e["category"]),
                    "message": str(e["message"])[:220],
                })
            return _json.dumps(clean)

    def update_telemetry_json(self, data_json: Optional[str] = None) -> None:
        """Receive a ThermalMonitor snapshot as a JSON string. Using JSON
        avoids Chaquopy's 'LinkedHashMap is not iterable' error that fires
        when Kotlin passes a Map directly and Python tries dict(map)."""
        import json as _json
        if data_json:
            try:
                self.telemetry = _json.loads(data_json)
            except Exception:
                pass

    def set_backend_url(self, url: str) -> None:
        """Re-point the model backend (STOP SERVER fallback semantics).

        When the on-device API server stops and a backup (desktop) URL is
        configured, the agent keeps running against the backup. Updates the
        live cached backend adapters and the model config in one place.
        """
        url = str(url or "").rstrip("/")
        if not url:
            return
        old = self.api_url
        self.api_url = url
        engine = self.engine
        if engine is not None:
            cache = getattr(engine, "_backend_cache", None)
            if isinstance(cache, dict):
                for backend in cache.values():
                    if hasattr(backend, "base_url"):
                        backend.base_url = url
            for model in getattr(engine, "models", None) or []:
                cfg = model.get("backend") if isinstance(model, dict) else None
                if isinstance(cfg, dict) and cfg.get("type") == "android":
                    cfg["api_url"] = url
        self.log("MODEL", f"backend re-pointed {old} -> {url}")

    def update_capabilities(self, declarations: Any) -> Dict[str, Any]:
        """Receive explicit capability declarations from the Android shell.

        This is the acknowledgement step of the capability contract:
        hardware -> permission -> capability declaration -> stream -> AGENT_ACK.
        Receiving a declaration never grants authority to use the capability.
        """
        if isinstance(declarations, dict):
            declarations = [declarations]
        acked: List[str] = []
        for decl in declarations or []:
            if not isinstance(decl, dict):
                continue
            name = str(decl.get("name", "")).strip().lower()
            if not name:
                continue
            self.capabilities[name] = {
                "state": str(decl.get("state", "unknown")),
                "permission": str(decl.get("permission", "unknown")),
                "stream": str(decl.get("stream", "off")),
                "detail": str(decl.get("detail", "")),
                "agent_ack": True,
                "ack_ts": time.time(),
            }
            acked.append(name)
        if acked:
            self.log("SENSOR", "capability declaration ack: " + ", ".join(acked))
        return {"acked": acked, "capabilities": self.get_capabilities()}

    def get_capabilities(self) -> Dict[str, Dict[str, Any]]:
        return {name: dict(decl) for name, decl in self.capabilities.items()}

    def update_policy(self, agent_caps: Optional[Dict[str, Any]] = None,
                      internet: Optional[bool] = None,
                      lan: Optional[bool] = None) -> Dict[str, Any]:
        """SECURITY-tab state: capability authority + network posture."""
        if isinstance(agent_caps, dict):
            self.agent_caps = {str(k): bool(v) for k, v in agent_caps.items()}
        if internet is not None:
            self.network_policy["internet"] = bool(internet)
        if lan is not None:
            self.network_policy["lan"] = bool(lan)
        self.log("POLICY", f"policy update: caps={self.agent_caps} "
                           f"net={self.network_policy}")
        return {"agent_caps": dict(self.agent_caps),
                "network": dict(self.network_policy)}

    def _backend_target_allowed(self) -> Tuple[bool, str]:
        """Enforce the network posture against the backend URL (fail closed)."""
        try:
            host = urllib.parse.urlparse(self.api_url).hostname or ""
        except Exception:
            return False, "unparseable"
        if host in ("127.0.0.1", "localhost", "::1"):
            return True, "localhost"
        parts = host.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            o0, o1 = int(parts[0]), int(parts[1])
            if o0 == 10 or (o0 == 192 and o1 == 168) or (o0 == 172 and 16 <= o1 <= 31):
                return bool(self.network_policy.get("lan")), "lan"
        if host.endswith(".local"):
            return bool(self.network_policy.get("lan")), "lan"
        return bool(self.network_policy.get("internet")), "internet"

    def update_telemetry(self, data: Optional[Dict[str, Any]] = None) -> None:
        """Receive a ThermalMonitor snapshot (Chaquopy Map -> dict)."""
        self.telemetry = dict(data) if data else self.telemetry or {}

    def _execute_engine_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Run one engine task with agent-level in-flight tracking.

        The Android shell schedules tick() at 1 Hz on a small thread pool; a
        long on-device generate (minutes of CPU prefill+decode) must not let
        queued ticks hammer the governor's re-entrancy interlock and flood
        the journal with governor_block refusals. Tracking lives HERE — not
        on governor internals — so it is immune to MagicMock engines in
        tests and works with any engine object.
        """
        import threading
        if not hasattr(self, "_task_gate"):
            self._task_gate = threading.Lock()
            self._task_in_flight = False
        with self._task_gate:
            if self._task_in_flight:
                return {"status": "in_flight"}
            self._task_in_flight = True
        try:
            return self.engine.execute_task(task)
        finally:
            self._task_in_flight = False

    def tick(self) -> None:
        self.tick_count += 1
        stages: List[str] = ["OBSERVE"]
        decision, action, evaluation = "—", "—", "—"
        try:
            observation = self._get_observation()
            self.last_observation = observation
            if self.memory is not None:
                self.memory.record_event("android_observation",
                    payload={"tick": self.tick_count, "observation": observation},
                    metadata={"source": "android_shell"})
            if self.engine is not None:
                allowed, scope = self._backend_target_allowed()
                if not allowed:
                    self.log("POLICY", f"backend {scope} '{self.api_url}' "
                                       f"blocked by network policy", level="WARN")
                    try:
                        self.memory.record_event("policy_block",
                            payload={"kind": "network",
                                     "reason": f"{scope} egress disabled"})
                    except Exception:
                        pass
                    stages.append("GATE")
                    decision = "blocked by network policy"
                    evaluation = "refused"
                else:
                    context = (self.memory.retrieve_context("maintain_agent_loop", top_k=3)
                               if self.memory is not None else {})
                    result = self._execute_engine_task(
                        {"id": f"android-tick-{self.tick_count}",
                         "type": "maintain_agent_loop", "context": context})
                    if str(result.get("status")) == "in_flight":
                        # A previous tick's task is still running; skip this
                        # cycle (OBSERVE-only) instead of piling a refused
                        # re-entrant attempt into the journal. The in-flight
                        # cycle journals its own outcome when it finishes.
                        evaluation = "waiting"
                        decision = "task in progress (previous tick)"
                        self.log("AGENT", f"tick {self.tick_count}: " +
                                 decision, level="DEBUG")
                    else:
                        stages += ["GATE", "DECIDE"]
                        status = str(result.get("status", "unknown"))
                        evaluation = status
                        if status == "success":
                            stages += ["EXECUTE", "EVALUATE", "RECORD"]
                            action = "executed"
                        elif status == "refused":
                            decision = str(result.get("reason", "refused"))[:90]
                            # The engine journals every refusal (policy_block /
                            # governor_block / fallback_halt): the cycle recorded.
                            stages.append("RECORD")
                        else:
                            decision = str(result.get("message", "error"))[:90]
                            # The engine journals the outcome (no_viable_action or
                            # task_failure) even when nothing executed.
                            stages.append("RECORD")
                        decision, action = self._enrich_from_memory(decision, action)
            else:
                decision = "no engine (backend unavailable)"
                if self.tick_count % 10 == 0:
                    self.log("AGENT", f"tick {self.tick_count}: no engine",
                             level="WARN")
            if self.tick_count % 10 == 0:
                stages.append("CONSOLIDATE")
                self._run_consolidation()
        except Exception as exc:
            logger.error("Tick %s error: %s", self.tick_count, exc)
            evaluation = f"error: {type(exc).__name__}"
        self._last_stages = stages
        self._last_decision = decision
        self._last_action = action
        self._last_evaluation = evaluation
        self._last_tick_ts = time.time()
        self.log("AGENT", f"cycle={self.tick_count} "
                          f"stages={'+'.join(stages)} eval={evaluation}")

    def _enrich_from_memory(self, decision: str, action: str) -> Tuple[str, str]:
        """Surface the real last decision/action from the Tier 1 head."""
        if self.memory is None:
            return decision, action
        try:
            for event in reversed(self.memory.tier1.recent(5)):
                etype = event.get("type")
                payload = event.get("payload") or {}
                if etype == "tool_execution":
                    return (str(payload.get("action_type", decision)),
                            f"tool_execution({payload.get('status', 'unknown')})")
                if etype == "policy_block":
                    return (f"blocked: {payload.get('kind', 'unknown')}",
                            f"policy_block({payload.get('reason', '')[:60]})")
                if etype == "task_failure":
                    return (decision, f"task_failure({payload.get('error', '')})")
        except Exception:
            pass
        return decision, action

    def _get_observation(self) -> Dict[str, Any]:
        """Observations from phone telemetry; safe stubs when none arrived."""
        t = self.telemetry or {}
        battery = t.get("battery_level", self._get_battery())
        mem_total = t.get("mem_total_mb", 0) or 0
        mem_avail = t.get("mem_avail_mb", 0) or 0
        memory_usage = (mem_total - mem_avail) if mem_total else self._get_memory_usage()
        return {
            "timestamp": t.get("timestamp_ms", time.time()), "battery": battery,
            "battery_plugged": t.get("is_charging", False), "memory_usage_mb": memory_usage,
            "memory_avail_mb": mem_avail, "cpu_temp_c": t.get("cpu_temp_c"),
            "accel": [t.get("accel_x", 0.0) or 0.0, t.get("accel_y", 0.0) or 0.0,
                      t.get("accel_z", 0.0) or 0.0],
            "thermal_state": t.get("thermal_state"),
        }

    def _get_battery(self) -> int:
        return 100

    def _get_memory_usage(self) -> int:
        return 0

    def _run_consolidation(self) -> None:
        try:
            self.memory.consolidate_now()
            self.log("MEMORY", "consolidation pass complete")
        except Exception as exc:
            logger.error("Consolidation error: %s", exc)

    def sensor_test_cycle(self, steps: int = 5) -> Dict[str, Any]:
        """Bounded OBSERVE->tick cycle; returns a structured report."""
        observations: List[Dict[str, Any]] = []
        for _ in range(steps):
            self.tick()
            observations.append(self._get_observation())
        status = self.get_status()
        status.update({"steps": steps, "observations": observations})
        return status

    @staticmethod
    def _tier2_count(memory: Any) -> int:
        count = getattr(memory.tier2, "count", None)
        try:
            return int(count()) if callable(count) else 0
        except Exception:
            return 0

    def get_status(self) -> Dict[str, Any]:
        mem = self.memory
        audit_active = (getattr(self.engine, "audit", None) is not None
                        if self.engine is not None else False)
        tier1 = (len(getattr(mem.tier1, "_events", []) or [])
                 if mem is not None else 0)
        tier0 = (len(getattr(mem.tier0, "_entries", []) or [])
                 if mem is not None else 0)
        return {
            # legacy keys (test_sensor_engagement / v1.8.1 UI)
            "tick_count": self.tick_count, "device_caps": self.device_caps,
            "engine": (self.engine.__class__.__name__ if self.engine else "None"),
            "tier1_entries": tier1,
            "telemetry_received": bool(self.telemetry),
            # control-plane surface
            "engine_ready": self.engine is not None,
            "engine_error": (self.engine_error or "")[:400],
            "init_error": (self.init_error or "")[:200],
            "data_dir": self.data_dir or "",
            "memory_usage_mb": self.last_observation.get("memory_usage_mb"),
            "tier0_entries": tier0,
            "tier2_facts": self._tier2_count(mem) if mem is not None else 0,
            "tier3": "READ ONLY",
            "pipeline_all": list(PIPELINE_STAGES),
            "pipeline_stages": list(self._last_stages),
            "last_decision": self._last_decision,
            "last_action": self._last_action,
            "last_evaluation": self._last_evaluation,
            "last_tick_ts": self._last_tick_ts,
            "backend_url": self.api_url,
            "capabilities": self.get_capabilities(),
            "recent_logs": self._log_buffer_snapshot(),
            "last_log_seq": self._log_seq,
            "policy": {"fail_closed": True, "audit": audit_active,
                       "consent_required": True,
                       "agent_caps": dict(self.agent_caps),
                       "network": dict(self.network_policy)},
        }

    def get_status_json(self) -> str:
        """JSON form of get_status — safe to cross the Chaquopy boundary
        (nested dicts/lists fail .toJava(Map) with ClassCastException)."""
        import json
        return json.dumps(self.get_status())

    def _log_buffer_snapshot(self) -> list:
        """Return a copy of the log buffer as a list of plain dicts."""
        with self._log_lock:
            return [dict(e) for e in self._log_buffer]

    def cleanup(self) -> None:
        try:
            self._run_consolidation()
        except Exception as exc:
            logger.error("Cleanup error: %s", exc)
        logger.info("Agent cleaned up")


def create_agent(device_caps: Optional[str] = None,
                 api_url: Optional[str] = None,
                 data_dir: Optional[str] = None) -> AndroidAgent:
    return AndroidAgent(device_caps=device_caps, api_url=api_url, data_dir=data_dir)
