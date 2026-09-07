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

# Human Interaction contract (v1.12). Defensive import: a construction
# failure here must degrade to a reporting agent, never kill the bootstrap.
try:
    from human_interaction import (AgentResponse, InteractionBus,
                                   HumanObservation, truncate_sentence)
    _HAS_INTERACTION = True
except Exception:  # pragma: no cover
    _HAS_INTERACTION = False

# v1.20 attention verification layer (provider-side, optional).
try:
    from attention_layer import AttentionLayer, AttentionState
    _HAS_ATTENTION = True
except Exception:
    _HAS_ATTENTION = False
from security import sanitize_text


logger = logging.getLogger("shugocore_android")

# The orchestration loop stages surfaced on the AGENT tab. Stage tracking is
# honest: a stage is only reported as run when the code path for it actually
# executed during the tick.
PIPELINE_STAGES = ("OBSERVE", "VERIFY_ATTENTION", "GATE", "DECIDE",
                   "EXECUTE", "EVALUATE", "RECORD", "CONSOLIDATE")

# -- cycle outcome contract (v1.10) -------------------------------------------
# Canonical cycle outcomes. NO_ACTION is NOT an error: "nothing to do" is a
# healthy, recorded cycle. BACKEND_FAILURE means the model ensemble could not
# be reached at all (transport class); NO_ACTION means the model answered but
# proposed nothing executable. The trail defaults below are the minimum
# honest stages; when the engine reports its own stage list (the governor
# steps that actually ran) that list prefixed with OBSERVE is shown instead.
# Every outcome ends in RECORD except IN_FLIGHT — the in-flight cycle
# journals its own outcome when it finishes.
CYCLE_OUTCOMES = ("SUCCESS", "NO_ACTION", "POLICY_BLOCK", "GOVERNOR_BLOCK",
                  "TASK_FAILURE", "IN_FLIGHT", "BACKEND_FAILURE",
                  "ENGINE_FAILURE")

_OUTCOME_TRAILS = {
    "SUCCESS": ("OBSERVE", "GATE", "DECIDE", "EXECUTE", "EVALUATE", "RECORD"),
    "NO_ACTION": ("OBSERVE", "GATE", "DECIDE", "RECORD"),
    "POLICY_BLOCK": ("OBSERVE", "GATE", "DECIDE", "RECORD"),
    "GOVERNOR_BLOCK": ("OBSERVE", "GATE", "RECORD"),
    "TASK_FAILURE": ("OBSERVE", "GATE", "DECIDE", "RECORD"),
    "IN_FLIGHT": ("OBSERVE",),
    "BACKEND_FAILURE": ("OBSERVE", "GATE", "DECIDE", "RECORD"),
    "ENGINE_FAILURE": ("OBSERVE",),
}

# Subconscious call-error classes indicating the model ensemble was
# unreachable (vs. reached-but-unhelpful, which is NO_ACTION).
_BACKEND_ERROR_CLASSES = ("transport_error", "model_unavailable", "invalid_model")

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
        # v1.21: LAN server URL (optional — the MacBook Ollama or another
        # local-network backend). When configured, the delegation manager routes
        # tasks by tier (LIGHT → localhost, STANDARD → localhost→LAN, etc.).
        self.lan_api_url: Optional[str] = None
        # v1.21: Network delegation manager — routes by scope, tier, and health.
        self.delegation = None  # set in _bootstrap() after import
        # Bounded seq-numbered log buffer the Kotlin UI polls (LogBus source).
        self._log_seq = 0
        self._log_buffer: deque = deque(maxlen=_LOG_BUFFER_MAX)
        self._log_lock = threading.Lock()
        # Human-interaction bus (v1.12 Interaction Foundation). Provider-side
        # contract: observations flow in here; the engine only ever sees
        # them as task `context` data (import-guard enforced in tests).
        self.interaction = InteractionBus() if _HAS_INTERACTION else None
        # v1.20 attention verification layer (provider-side).
        self.attention = AttentionLayer() if _HAS_ATTENTION else None
        # Last-tick decision source for cycle-truth observability.
        self._decision_source: str = "none"
        # v1.20: dedup cursor for conversation memory storage.
        self._last_stored_turn_count: int = 0
        self._last_stages: List[str] = []
        self._last_decision = "—"
        self._last_action = "—"
        self._last_evaluation = "—"
        self._last_tick_ts = 0.0
        self._last_cycle_result: Optional[Dict[str, Any]] = None
        # Any construction failures, captured so the UI can surface the real
        # reason instead of a silent zombie agent (cycle=0 forever).
        self.engine_error: Optional[str] = None
        self.init_error: Optional[str] = None
        self.memory = None
        self.capability_registry = None
        self.consent_registry = None
        self.engine: Optional[Any] = None
        # Speech-output provider (v1.15): the Kotlin TTS bridge attaches via
        # register_speak_listener(); the internal speak action executes
        # through it. The decision core never imports the provider.
        self._speak_listener: Optional[Any] = None
        self._bootstrap()
        if self.engine is not None:
            try:
                self.engine.execution_layer.register_handler(
                    "speak", self._execute_speak)
                self.engine.execution_layer.register_handler(
                    "ask_user", self._execute_ask_user)
            except Exception as exc:
                self.log("ERROR", f"speech handler registration failed: {exc}",
                         level="ERROR")
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
                                            semantic=semantic,
                                            episodic_journal_path=self.episodic_journal_path)
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
            # v1.20: start ShugoNet runtime for cross-device transport.
            self._start_shugonet()
            # v1.21: initialise the network delegation manager and register
            # known backends.  Best-effort: failures degrade gracefully.
            try:
                from delegation import BackendScope, DelegationManager, TaskTier
                self.delegation = DelegationManager()
                self.delegation.register(BackendScope.LOCALHOST, self.api_url,
                                         backend_type="android",
                                         task_tiers=frozenset({TaskTier.LIGHT,
                                                               TaskTier.STANDARD}))
                if self.lan_api_url:
                    self.delegation.register(BackendScope.LAN, self.lan_api_url,
                                             backend_type="ollama",
                                             task_tiers=frozenset({TaskTier.STANDARD,
                                                                   TaskTier.HEAVY}))
                self.log("AGENT", "delegation manager ready")
            except Exception as exc:
                self.log("AGENT", f"delegation init skipped: {exc}", level="WARN")
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
    def _start_shugonet(self) -> None:
        """v1.20: start the ShugoNet TCP/JSON transport runtime and register
        network handlers with the execution layer. Best-effort: failures
        degrade gracefully (log a warning, no crash)."""
        self.shugonet_runtime = None
        # Check if already running (prevents duplicate starts on restart)
        if getattr(self, "_shugonet_started", False):
            self.log("AGENT", "shugonet runtime already running")
            return
        try:
            from agent_runtime import ShugonetAgentRuntime
            from shugonet_bridge import register_network_handlers
            self.shugonet_runtime = ShugonetAgentRuntime(
                agent_id=f"shugo-{self.device_caps or 'android'}",
                host="0.0.0.0", port=9000)
            self.shugonet_runtime.start()
            if self.engine is not None:
                register_network_handlers(
                    self.engine.execution_layer, self.shugonet_runtime)
            self.log("AGENT", f"shugonet runtime started on port 9000")
            self._shugonet_started = True
            import sys as _sys
            print("SHUGONET: runtime started on port 9000", file=_sys.stderr, flush=True)
        except Exception as exc:
            self.log("AGENT", f"shugonet start skipped: {exc}", level="WARN")
            import sys as _sys
            print(f"SHUGONET: start failed: {exc}", file=_sys.stderr, flush=True)
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
            # Share the agent's MemoryManager: observations (agent) and
            # decisions/executions (engine) must land in ONE Tier 1 ring so
            # the control plane's enrichment and the journal are complete.
            if self.memory is not None:
                kwargs["memory"] = self.memory
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

    def probe_model(self) -> Dict[str, Any]:
        """MODEL TEST: one controlled decision-prompt round-trip with full
        visibility. Distinguishes the three failure classes:
          A) no_response      — the model ensemble was unreachable / silent
          B) invalid_protocol — a response came back but did not parse
          C) valid_protocol   — parsed (action_type may still be null)"""
        started = time.time()
        probe: Dict[str, Any] = {"ok": False, "error_class": "no_response",
                                 "raw": "", "parse_valid": False,
                                 "action_type": "", "confidence": 0.0,
                                 "latency_ms": 0, "chars": 0, "model": ""}
        engine = self.engine
        if engine is None:
            probe["raw"] = "no engine constructed"
            return probe
        sub = getattr(engine, "subconscious", None)
        if sub is None:
            probe["raw"] = "engine has no subconscious"
            return probe
        try:
            models = engine.select_models({"type": "model_probe"}) or []
            if not models:
                probe["raw"] = "no models selected"
                return probe
            model = models[0]
            probe["model"] = str(model.get("id", ""))
            output = sub.get_model_output(
                probe["model"],
                {"type": "model_probe", "content": "routine agent observation"},
                backend=engine._backend_for(model),
                action_schema=engine.available_action_types())
            probe["latency_ms"] = int((time.time() - started) * 1000)
            probe["chars"] = len(output or "")
            if not output:
                probe["raw"] = "(empty response)"
                return probe                          # class A: no response
            probe["raw"] = str(output)[:400]
            parsed = engine._parse_proposal(output)
            if parsed is None:
                probe["error_class"] = "invalid_protocol"   # class B
                return probe
            probe["parse_valid"] = True                          # class C
            probe["ok"] = True
            probe["error_class"] = ("valid_protocol_null"
                                    if parsed.get("action_type") is None
                                    else "valid_protocol")
            probe["action_type"] = str(parsed.get("action_type") or "null")
            probe["confidence"] = float(parsed.get("confidence") or 0.0)
            return probe
        except Exception as exc:
            probe["latency_ms"] = int((time.time() - started) * 1000)
            probe["error_class"] = "no_response"
            probe["raw"] = f"{type(exc).__name__}: {exc}"[:200]
            return probe

    def probe_model_json(self) -> Dict[str, Any]:
        """JSON form of probe_model (primitive-only values cross Chaquopy)."""
        return self.probe_model()

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

    # -- v1.15 speech output -------------------------------------------------

    def register_speak_listener(self, listener: Any) -> None:
        """Attach the edge speech-output provider (the Kotlin TTS bridge).
        The decision core stays provider-agnostic: it only ever proposes the
        internal ``speak`` action; this listener is the executor."""
        self._speak_listener = listener

    def _execute_speak(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Executor for the internal speak action. Text is sanitized and
        bounded here; the listener (Kotlin TTS) performs the actual output.
        The AgentResponse lands on the interaction bus so the UI and telemetry
        tell the truth about what the agent said."""
        params = decision.get("params") or {}
        # Dialect tolerance: small models put the words in params.text,
        # params.utterance, or (after parser normalization) top-level text
        # routed into params.utterance. Accept any, prefer text.
        # v1.18: sentence-aware bound — the agent never speaks a fragment.
        text = truncate_sentence(
            str(params.get("text") or params.get("utterance") or ""), 400)
        if not text:
            return {"status": "refused", "reason": "empty speech content"}
        listener = self._speak_listener
        if listener is None:
            return {"status": "no_output",
                    "reason": ("no speech provider attached; actions are "
                               "never simulated")}
        try:
            delivered = bool(listener.speak(text))
        except Exception as exc:
            self.log("ERROR", f"speak listener failed: {type(exc).__name__}",
                     level="ERROR")
            return {"status": "error", "message": type(exc).__name__}
        if self.interaction is not None:
            self.interaction.record_agent_response(AgentResponse(
                type="speech", content=text, target="user"))
        return {"status": "success", "spoken": text, "delivered": delivered}

    def _execute_ask_user(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Executor for the v1.16 ask_user action: the agent is uncertain and
        ASKS the operator instead of guessing (OBSERVE -> GATE -> ASK USER ->
        LISTEN -> DECIDE). Same TTS edge as speak; the question is journaled,
        marked on the bus as expecting an answer, and the next speech
        observation within the TTL is paired with it. A spoken reply is DATA
        the agent may reason over — it is never a consent record."""
        params = decision.get("params") or {}
        # v1.18: sentence-aware bound (never a spoken fragment).
        text = truncate_sentence(
            str(params.get("question") or params.get("text")
                or params.get("utterance") or ""), 400)
        if not text:
            return {"status": "refused", "reason": "empty question"}
        listener = self._speak_listener
        if listener is None:
            return {"status": "no_output",
                    "reason": ("no speech provider attached; actions are "
                               "never simulated")}
        try:
            delivered = bool(listener.speak(text))
        except Exception as exc:
            self.log("ERROR", f"ask_user listener failed: {type(exc).__name__}",
                     level="ERROR")
            return {"status": "error", "message": type(exc).__name__}
        if self.interaction is not None:
            self.interaction.record_agent_response(AgentResponse(
                type="speech", content=text, target="user",
                expects_answer=True))
        if self.memory is not None:
            self.memory.record_event(
                "agent_question", {"question": text})
        return {"status": "success", "asked": text, "delivered": delivered}

    def speak_test(self, text: Optional[str] = None) -> Dict[str, Any]:
        """Operator control (AGENT tab 'Test speech'): drives one speak
        action through the REAL policy gate + execution path."""
        content = sanitize_text(
            str(text or "I am here. Shugo can speak."), 200)
        self.log("AGENT", f"speak_test: {content!r}")
        if self.engine is None:
            return {"status": "error", "message": "engine unavailable"}
        decision = {"action_type": "speak",
                    "params": {"text": content},
                    "confidence": 1.0,
                    "proposal_source": "operator_test"}
        return self.engine._execute_gated(decision)

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

    def publish_human_observation(
            self, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Ingest one HumanObservation from a provider (Kotlin edge, tests,
        or any future wearable). Sanitize → validate → bus → Tier 1. The
        event is memory-internal by construction (like record_observation):
        no consent, no approval, no egress — the payload never leaves the
        device and only type/source/confidence reach the journal."""
        if self.interaction is None:
            return {"accepted": False, "reason": "interaction bus unavailable"}
        obs, reason = HumanObservation.from_dict(
            data if isinstance(data, dict) else {})
        if obs is None:
            self.interaction.record_rejected()
            return {"accepted": False, "reason": reason}
        accepted, detail = self.interaction.publish(obs)
        if not accepted:
            return {"accepted": False, "reason": detail}
        if self.memory is not None:
            try:
                self.memory.record_event(
                    "human_observation",
                    payload={"type": obs.type, "source": obs.source,
                             "confidence": obs.confidence},
                    metadata={"source": obs.source,
                              "privacy_scope": obs.privacy_scope})
            except Exception as exc:
                logger.warning("human_observation record failed: %s", exc)
        self.log("INTERACTION", f"{obs.type} from {obs.source}: {detail}")
        return {"accepted": True, "type": obs.type, "detail": detail,
                "presence_event":
                    detail if detail.startswith("USER_") else ""}

    def publish_human_observation_json(
            self, data_json: Optional[str] = None) -> str:
        """JSON twin of publish_human_observation for the Chaquopy boundary
        (same rationale as update_telemetry_json / get_status_json)."""
        import json as _json
        data: Optional[Dict[str, Any]] = None
        if data_json:
            try:
                data = _json.loads(data_json)
            except Exception:
                data = None
        return _json.dumps(self.publish_human_observation(data))

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
        # v1.21: also update the delegation manager's LOCALHOST entry.
        if self.delegation is not None:
            try:
                from delegation import BackendScope
                self.delegation.register(BackendScope.LOCALHOST, url,
                                         backend_type="android")
            except Exception:
                pass

    def set_lan_url(self, url: Optional[str]) -> None:
        """v1.21: configure the LAN backend URL (e.g. MacBook Ollama).

        Registers or updates the LAN-scope entry in the delegation manager.
        Pass ``None`` or empty string to remove the LAN entry.
        """
        url = str(url or "").rstrip("/") if url else None
        old = getattr(self, "lan_api_url", None)
        self.lan_api_url = url
        if self.delegation is None:
            return
        try:
            from delegation import BackendScope, TaskTier
            if url:
                self.delegation.register(BackendScope.LAN, url,
                                         backend_type="ollama",
                                         task_tiers=frozenset({TaskTier.STANDARD,
                                                               TaskTier.HEAVY}))
                self.log("MODEL", f"LAN backend set: {url} (was {old})")
            else:
                # Unregister LAN backend if URL was cleared.
                if old:
                    self.delegation.unregister(BackendScope.LAN, old)
                self.log("MODEL", "LAN backend cleared")
        except Exception as exc:
            self.log("MODEL", f"LAN backend update skipped: {exc}", level="WARN")

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
        decision, action = "—", "—"
        outcome = "ENGINE_FAILURE"
        trail: Tuple[str, ...] = ("OBSERVE",)
        detail = "cycle did not complete"
        executed = False
        self._decision_source = "none"
        try:
            observation = self._get_observation()
            self.last_observation = observation
            if self.memory is not None:
                self.memory.record_event("android_observation",
                    payload={"tick": self.tick_count, "observation": observation},
                    metadata={"source": "android_shell"})
            # v1.17 closed-loop Record: completed question/answer round trips
            # land in Tier 1 as METADATA ONLY (latency + lengths — the words
            # stay in the bounded bus, per the transcripts-never-enter-memory
            # rule established with the 1.12 journal contract).
            if self.interaction is not None:
                for event in self.interaction.drain_conversation_events():
                    if self.memory is None:
                        break
                    try:
                        self.memory.record_event("conversation_event",
                            payload={
                                "round_trip_s": event.get("round_trip_s"),
                                "question_chars":
                                    len(str(event.get("question") or "")),
                                "answer_chars":
                                    len(str(event.get("answer") or "")),
                            },
                            metadata={"source": "interaction_bus",
                                      "privacy_scope": "local"})
                    except Exception:
                        pass
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
                    outcome = "POLICY_BLOCK"
                    trail = ("OBSERVE", "GATE", "RECORD")
                    detail = f"network: {scope} egress disabled"
                    decision = "blocked by network policy"
                else:
                    context = (self.memory.retrieve_context("maintain_agent_loop", top_k=3)
                               if self.memory is not None else {})
                    # v1.19: inject the causal ID chain into the decision
                    # context so the policy token binds the trace.
                    if self.interaction is not None:
                        istats = self.interaction.stats()
                        context["conversation_id"] = istats.get("conversation_id")
                        context["turn_id"] = istats.get("current_turn_id")
                    # v1.20: inject attention verdict into task context
                    if self.attention is not None:
                                        if self.interaction is not None:
                                                            human = observation.get("human", {})
                                                            if human.get("speech_recent"):
                                                                                t = (human.get("user_context", {}).get("last_transcript") or "")
                                                                                self.attention.stamp_speech(t)
                                                            lv = next((e for e in reversed(getattr(self.interaction, "_buffer", [])) if e.get("type") == "visual"), None)
                                                            if lv:
                                                                                fc = lv.get("payload", {}).get("face_count", 0)
                                                                                self.attention.stamp_face(fc if isinstance(fc, (int, float)) else 0)
                                                            self.attention.stamp_tts(observation.get("tts_speaking", False))
                                        att_state, att_conf = self.attention.evaluate()
                                        context["attention_state"] = str(att_state.value) if hasattr(att_state, "value") else str(att_state)
                                        context["attention_confidence"] = round(att_conf, 2)
                    # v1.20: inject conversation history into context
                    if self.interaction is not None:
                        turns = self.interaction.export_conversation_turns()
                        if turns:
                            context["conversation_history"] = turns
                            lines = []
                            for t in turns[-8:]:
                                role = t.get("role", "?").upper()[:5]
                                text = t.get("text", "")[:120]
                                lines.append(f"{role}: {text}")
                            context["conversation_summary"] = "\n".join(lines)
                    # v1.21: delegate to the best backend for this task.
                    # The delegation manager routes by task tier, network
                    # policy, and health.  The selected URL is injected into
                    # the task context so the decision engine's _backend_for()
                    # can use it for model inference.
                    if self.delegation is not None:
                        try:
                            del_url, del_scope = self.delegation.select_backend(
                                "maintain_agent_loop",
                                self.network_policy,
                                consent_has_delegate_internet=(
                                    self.agent_caps.get("delegate_internet", False)
                                    if hasattr(self, "agent_caps") else False),
                            )
                            if del_url:
                                context["delegation_url"] = del_url
                                context["delegation_scope"] = del_scope or "localhost"
                        except Exception:
                            pass
                    engine_result = self._execute_engine_task(
                        {"id": f"android-tick-{self.tick_count}",
                         "type": "maintain_agent_loop", "context": context})
                    if str(engine_result.get("status")) == "in_flight":
                        # A previous tick's task is still running; skip this
                        # cycle (OBSERVE-only) instead of piling a refused
                        # re-entrant attempt into the journal. The in-flight
                        # cycle journals its own outcome when it finishes.
                        outcome = "IN_FLIGHT"
                        trail = ("OBSERVE",)
                        detail = "previous cycle still in flight"
                        decision = "task in progress (previous tick)"
                        self.log("AGENT", f"tick {self.tick_count}: " +
                                 decision, level="DEBUG")
                    else:
                        (outcome, trail, detail, executed,
                         decision) = self._classify_engine_result(engine_result)
                        if outcome == "SUCCESS":
                            action = "executed"
                        decision, action = self._enrich_from_memory(decision, action)
            # v1.20: store conversation turns in Tier-2 memory regardless of
            # outcome — NO_ACTION (null decision), POLICY_BLOCK, TASK_FAILURE,
            # and SUCCESS all persist the conversation for future recall.
            if self.memory is not None and self.interaction is not None:
                self._store_conversation_memory()
            else:
                outcome = "ENGINE_FAILURE"
                detail = "no engine constructed"
                decision = "no engine (backend unavailable)"
                if self.tick_count % 10 == 0:
                    self.log("AGENT", f"tick {self.tick_count}: no engine",
                             level="WARN")
            if self.tick_count % 10 == 0:
                self._run_consolidation()
                trail = trail + ("CONSOLIDATE",)
        except Exception as exc:
            logger.error("Tick %s error: %s", self.tick_count, exc)
            outcome = "ENGINE_FAILURE"
            trail = ("OBSERVE",)
            detail = f"{type(exc).__name__}: {exc}"[:120]
        self._last_stages = list(trail)
        self._last_cycle_result = {"outcome": outcome, "stages": list(trail),
                                   "detail": detail, "executed": executed,
                                   "decision_source": self._decision_source}
        self._last_decision = decision
        self._last_action = action
        self._last_evaluation = outcome.lower()
        self._last_tick_ts = time.time()
        self.log("AGENT", f"cycle={self.tick_count} outcome={outcome} "
                          f"stages={'+'.join(trail)}")

    def _classify_engine_result(self, engine_result: Dict[str, Any]
                                ) -> Tuple[str, Tuple[str, ...], str, bool, str]:
        """Map an execute_task result onto the cycle outcome contract.

        Returns (outcome, trail, detail, executed, decision_summary). The
        trail is the engine's own stage list (the governor steps that
        actually ran) when provided; otherwise the canonical minimum trail
        for the outcome. EXECUTE/EVALUATE are only ever claimed because the
        engine reported them or the result is a real success.
        """
        status = str(engine_result.get("status", "unknown"))
        outcome_raw = str(engine_result.get("outcome") or "")
        if not outcome_raw:
            # Legacy / mocked results without an explicit outcome: derive
            # from status + message (same semantics as v1.9.0).
            message = str(engine_result.get("message",
                                            engine_result.get("reason", "")))
            if status == "success":
                outcome_raw = "success"
            elif status == "refused":
                outcome_raw = "policy_block"
            elif "no viable action" in message:
                outcome_raw = "no_viable_action"
            else:
                outcome_raw = "task_failure"

        if outcome_raw == "success":
            outcome = "SUCCESS"
            decision = "executed"
        elif outcome_raw == "no_viable_action":
            call_errors = engine_result.get("call_errors") or {}
            backend_down = any(
                str(cls).split(":")[0] in _BACKEND_ERROR_CLASSES
                for cls in call_errors.values())
            outcome = "BACKEND_FAILURE" if backend_down else "NO_ACTION"
            decision = str(engine_result.get("message", "no viable action"))[:90]
        elif outcome_raw == "policy_block":
            outcome = "POLICY_BLOCK"
            decision = f"blocked: {engine_result.get('reason', 'gated')}"[:90]
        elif outcome_raw == "governor_block":
            outcome = "GOVERNOR_BLOCK"
            decision = f"blocked: {engine_result.get('reason', 'governor')}"[:90]
        else:  # task_failure and anything unrecognized
            outcome = "TASK_FAILURE"
            decision = str(engine_result.get("message", status))[:90]

        engine_stages = engine_result.get("stages")
        if isinstance(engine_stages, (list, tuple)) and engine_stages:
            trail = tuple(["OBSERVE"] + [str(stage) for stage in engine_stages])
        else:
            trail = _OUTCOME_TRAILS.get(outcome, ("OBSERVE",))
        detail = str(engine_result.get("result_status")
                     or engine_result.get("reason")
                     or engine_result.get("message") or "")[:120]
        executed = status == "success" or bool(engine_result.get("executed"))
        return outcome, trail, detail, executed, decision

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
        """Observations from phone telemetry + the human-interaction bus;
        safe stubs when none arrived."""
        t = self.telemetry or {}
        battery = t.get("battery_level", self._get_battery())
        mem_total = t.get("mem_total_mb", 0) or 0
        mem_avail = t.get("mem_avail_mb", 0) or 0
        memory_usage = (mem_total - mem_avail) if mem_total else self._get_memory_usage()
        observation = {
            "timestamp": t.get("timestamp_ms", time.time()), "battery": battery,
            "battery_plugged": t.get("is_charging", False), "memory_usage_mb": memory_usage,
            "memory_avail_mb": mem_avail, "cpu_temp_c": t.get("cpu_temp_c"),
            "accel": [t.get("accel_x", 0.0) or 0.0, t.get("accel_y", 0.0) or 0.0,
                      t.get("accel_z", 0.0) or 0.0],
            "thermal_state": t.get("thermal_state"),
        }
        if self.interaction is not None:
            # Human context rides into DECIDE as task context data — the
            # provider rule: the core never imports the interaction module.
            hctx = self.interaction.human_context() or {}
            # v1.19 multimodal fusion: enrich with Tier-2 entity-graph
            # facts (the "Markus was doing Y" leg).
            if self.memory is not None:
                transcript = (hctx.get("user_context", {})
                             .get("last_transcript") or "")
                if transcript:
                    try:
                        entities = self.memory.tier2.extract_entities(
                            transcript)[:3]
                        facts = []
                        for ent in entities:
                            facts.extend(
                                self.memory.tier2.facts_about(
                                    ent, limit=2))
                        if facts:
                            hctx.setdefault("user_context", {})
                            hctx["user_context"]["entity_facts"] = facts
                    except Exception:
                        pass
            observation["human"] = hctx
        # v1.22: device mesh peer observations from connected peripherals.
        # The Kotlin service pushes mesh peer JSON into telemetry.
        mesh = t.get("mesh_peers", None)
        if mesh is not None:
            try:
                if isinstance(mesh, str):
                    import json as _json
                    mesh = _json.loads(mesh)
                if isinstance(mesh, list):
                    observation["mesh_peers"] = mesh
                    observation["mesh_peer_count"] = len(mesh)
                    for peer in mesh:
                        # Inject remote camera observations into the bus
                        if peer.get("camera") and self.interaction is not None:
                            from human_interaction import HumanObservation
                            obs, _ = HumanObservation.from_dict({
                                "type": "visual",
                                "source": f"remote:{peer.get('device_id', 'unknown')}",
                                "payload": {"person_present": True, "face_count": 1},
                                "privacy_scope": "local",
                            })
                            if obs is not None:
                                self.interaction.publish(obs)
            except Exception:
                pass
        return observation

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

    def _store_conversation_memory(self) -> None:
        """v1.20: persist NEW conversation turns into Tier-2 semantic memory.
        Uses a cursor (_last_stored_turn_count) to avoid storing the same
        turn across multiple ticks — only stores turns that have been added
        since the last call."""
        try:
            turns = self.interaction.export_conversation_turns()
            if not turns:
                return
            # Only store new turns since last call (dedup by index).
            cursor = getattr(self, "_last_stored_turn_count", 0)
            if len(turns) <= cursor:
                return
            stored = 0
            for turn in turns[cursor:]:  # only new turns
                role = turn.get("role", "?")
                text = turn.get("text", "")
                if not text:
                    continue
                content = f"[{role.upper()}] {text}"
                conv_id = turn.get("conversation_id",
                                   turn.get("response_id", ""))
                self.memory.tier2.store_fact(
                    content=content[:500],
                    kind="conversation_turn",
                    salience=0.6,
                    metadata={"role": role,
                              "conversation_id": str(conv_id)},
                )
                stored += 1
            self._last_stored_turn_count = len(turns)
            if stored > 0:
                self.log("MEMORY", f"stored {stored} new conversation turn(s) in Tier-2")
        except Exception as exc:
            logger.debug("Conversation memory store skipped: %s", exc)

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

    @staticmethod
    def _recent_conversation_facts(memory: Any, limit: int = 5) -> List[Dict[str, Any]]:
        """v1.20: return recent conversation turns from Tier-2 memory."""
        try:
            # Query a larger pool so the kind filter doesn't miss results.
            facts = memory.tier2.search("conversation", top_k=20, min_salience=0.1)
            # Filter to only conversation_turn kind
            conv_facts = [f for f in facts if f.get("kind") == "conversation_turn"]
            return [{"content": f["content"][:200], "salience": round(f.get("salience", 0), 2),
                     "created_at": f.get("created_at", "")} for f in conv_facts]
        except Exception:
            return []

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
            # v1.20: recent conversation turns stored in Tier-2 memory
            "conversation_memory": self._recent_conversation_facts(mem) if mem is not None else [],
            "tier3": "READ ONLY",
            "pipeline_all": list(PIPELINE_STAGES),
            "pipeline_stages": list(self._last_stages),
            "last_decision": self._last_decision,
            "last_action": self._last_action,
            "last_evaluation": self._last_evaluation,
            "last_cycle_result": self._last_cycle_result,
            "last_tick_ts": self._last_tick_ts,
            # v1.18: face/liveness signal — true while a decision cycle is
            # between start and finish (drives the THINKING face state).
            # getattr: the gate is created lazily on the first engine task.
            "task_in_flight": bool(getattr(self, "_task_in_flight", False)),
            "backend_url": self.api_url,
            "capabilities": self.get_capabilities(),
            "interaction": (self.interaction.stats()
                            if self.interaction is not None else None),
            # v1.17 closed-loop validation: one liveness view over the whole
            # pipeline (sensors/vision/hearing/speech/model/memory), rendered
            # as the AGENT tab Validation section.
            "pipeline": (self.interaction.pipeline_health(
                model_ready=self.engine is not None,
                tts_attached=self._speak_listener is not None,
                memory_ok=self.memory is not None)
                if self.interaction is not None else None),
            "recent_logs": self._log_buffer_snapshot(),
            "last_log_seq": self._log_seq,
            # v1.20: cycle-truth observability — what drove the last output.
            "decision_source": self._decision_source,
            "attention": (self.attention.snapshot()
                          if self.attention is not None else None),
            # v1.21: network delegation — active scope, URL, and backends.
            "delegation": (self.delegation.status()
                           if self.delegation is not None else None),
            "lan_api_url": self.lan_api_url or "",
            # v1.22: device mesh — connected peripheral devices and their
            # sensor status (from the last observation).
            "mesh_peer_count": self.last_observation.get("mesh_peer_count", 0),
            "mesh_peers": self.last_observation.get("mesh_peers", []),
            "policy": {"fail_closed": True, "audit": audit_active,
                       "consent_required": True,
                       "agent_caps": dict(self.agent_caps),
                       "network": dict(self.network_policy)},
        }

    def get_attention_state_json(self) -> str:
        """v1.20: lightweight attention state for the Kotlin service to set
        camera attention mode. Returns JSON with 'active' (bool) and
        'state' (string)."""
        import json
        if self.attention is None:
            return json.dumps({"active": False, "state": "unavailable"})
        snap = self.attention.snapshot()
        return json.dumps({
            "active": snap.get("state") in ("attending", "unknown"),
            "state": snap.get("state", "unknown"),
        })

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
        # v1.20: stop ShugoNet runtime.
        try:
            if getattr(self, "shugonet_runtime", None) is not None:
                self.shugonet_runtime.stop()
        except Exception:
            pass
        logger.info("Agent cleaned up")


def create_agent(device_caps: Optional[str] = None,
                 api_url: Optional[str] = None,
                 data_dir: Optional[str] = None) -> AndroidAgent:
    return AndroidAgent(device_caps=device_caps, api_url=api_url, data_dir=data_dir)
