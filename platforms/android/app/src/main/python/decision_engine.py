"""
ShugoCore decision engine
=========================

Single gated execution path. Every task - interactive or autonomous - flows
through ``execute_task``:

1. ``check_ethics``: Tier 3 world-model invariants + rule stack.
2. ``make_decision``: models emit structured JSON proposals; the highest
   confidence-weighted valid proposal becomes the decision, enriched with
   Tier 2 memory context.
3. Action-level gate: Tier 3 checks the *decision*, the ConsentRegistry
   governs side-effecting actions, the ApprovalBroker enforces
   human-in-the-loop for side effects (fail-closed).
4. A policy verdict token - bound to the canonical hash of the decision - is
   attached; the execution layer refuses decisions without a matching token.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    import torch
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

from audit import AuditChain
from autonomy import Autonomy
from execution_layer import ExecutionLayer
from fallbacks import FallbackController, FallbackHalt
from logging_manager import LoggingManager
from memory_system import CoreIdentity, MemoryManager, SemanticMemory
from model_backends import create_backend, validate_model_name
from model_manager import ModelManager
from policy import (
    OBSERVATION_ACTION_TYPES,
    SIDE_EFFECTING_ACTION_TYPES,
    ApprovalBroker,
    CapabilityRegistry,
    ConsentRegistry,
)

_pg_memory_fn = None  # Lazily imported — pg_memory pulls in psycopg2 which is
                      # not bundled in the Android build. Android persists
                      # events via the audit JSONL chain, not Postgres.


def _get_pg_memory():
    global _pg_memory_fn
    if _pg_memory_fn is None:
        try:
            from pg_memory import open_semantic_memory
            _pg_memory_fn = open_semantic_memory
        except Exception:
            _pg_memory_fn = False  # mark unavailable; don't retry
    return _pg_memory_fn
from reinforcement_learning import ReinforcementLearning
from security import (
    RateLimiter,
    SecretResolver,
    canonical_hash,
    redact,
    sanitize_text,
)
from state_machine import AgentState, ExecutionGovernor, GovernorError
from subconscious import SubconsciousModel
from task_manager import TaskManager
from telemetry import get_tracer
from token_budget import ContextBudget, estimate_tokens
from vector_db import VectorDB

try:
    from robotics_handler import (
        RoboticsExecutionHandler,
        ROBOTICS_ACTION_TYPES,
        ROBOTICS_SAFETY_ACTION_TYPES,
        ROBOTICS_READ_ACTION_TYPES,
    )
    _HAS_ROBOTICS = True
except ImportError:
    _HAS_ROBOTICS = False
    ROBOTICS_ACTION_TYPES = frozenset()
    ROBOTICS_SAFETY_ACTION_TYPES = frozenset()
    ROBOTICS_READ_ACTION_TYPES = frozenset()

try:
    from mobile_nodes import (
        MobileExecutionHandler,
        MOBILE_ACTION_TYPES,
        MOBILE_READ_ACTION_TYPES,
    )
    _HAS_MOBILE = True
except ImportError:
    _HAS_MOBILE = False
    MOBILE_ACTION_TYPES = frozenset()
    MOBILE_READ_ACTION_TYPES = frozenset()

try:
    from shugonet_bridge import (
        ShugonetExecutionHandler,
        NETWORK_ACTION_TYPES,
        NETWORK_READ_ACTION_TYPES,
    )
    _HAS_SHUGONET = True
except ImportError:
    _HAS_SHUGONET = False
    NETWORK_ACTION_TYPES = frozenset()
    NETWORK_READ_ACTION_TYPES = frozenset()

logger = logging.getLogger(__name__)

_PROPOSAL_JSON_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def _proposal_json_candidates(text: str):
    """
    Yield candidate JSON blobs from model output, most specific first.

    Small on-device models frequently wrap the JSON (prose before/after) or
    truncate it at the token cap; greedy ``{...}`` matching on the full text
    grabs everything from the first to the last brace, which fails when the
    trailing prose contains its own braces. Strategy:

    1. Greedy first-brace-to-last-brace blob (backwards compatible).
    2. Each balanced ``{...}`` region starting at every ``{`` (handles prose
       after the object and nested braces).
    3. The longest prefix that parses as a dict (handles truncated JSON where
       the tail is cut mid-value).
    """
    m = _PROPOSAL_JSON_PATTERN.search(text)
    if m:
        yield m.group(0)
    for m in re.finditer(r"\{", text):
        depth = 0
        for i in range(m.start(), len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[m.start():i + 1]
                    break
    # Truncated-at-token-cap repair: if the merged text starts with '{' and is
    # unterminated but already carries an action_type key, close the still-open
    # object braces deterministically and try it last.
    stripped = text.lstrip()
    if stripped.startswith("{") and "action_type" in stripped[:96]:
        net = 0
        for ch in stripped:
            if ch == "{":
                net += 1
            elif ch == "}":
                net -= 1
        if net > 0:
            yield stripped + ("}" * net)
    for cut in range(len(text), 0, -8):
        try:
            data = json.loads(text[:cut])
        except Exception:
            continue
        if isinstance(data, dict):
            yield text[:cut]
            break

_KNOWN_ACTION_TYPES = {
    "api_call", "database_update", "hardware_interaction",
    "news_api", "search_api", "multi_step_process",
    "record_observation",
}
if _HAS_ROBOTICS:
    _KNOWN_ACTION_TYPES |= (ROBOTICS_ACTION_TYPES
                            | ROBOTICS_SAFETY_ACTION_TYPES
                            | ROBOTICS_READ_ACTION_TYPES)
if _HAS_MOBILE:
    _KNOWN_ACTION_TYPES |= MOBILE_ACTION_TYPES | MOBILE_READ_ACTION_TYPES
if _HAS_SHUGONET:
    _KNOWN_ACTION_TYPES |= NETWORK_ACTION_TYPES | NETWORK_READ_ACTION_TYPES
# Compute offload leaves the host and runs on a personal device: consent-
# gated exactly like the other side-effecting actions.
_NETWORK_CONSENT_GATED = NETWORK_ACTION_TYPES if _HAS_SHUGONET else frozenset()
_CONSENT_GATED_ACTION_TYPES = (SIDE_EFFECTING_ACTION_TYPES
                               | (MOBILE_ACTION_TYPES if _HAS_MOBILE
                                  else frozenset())
                               | _NETWORK_CONSENT_GATED)


def _governor_trigger_kind(exc: GovernorError) -> str:
    """Map a governor interlock to the fallback controller's trigger kind."""
    message = str(exc).lower()
    if "step budget" in message:
        return "step_budget_exhausted"
    if "deadline" in message:
        return "task_deadline_exceeded"
    if "re-entrant" in message or "paused" in message or "halted" in message:
        return "runaway_loop"
    return "runaway_loop"


class DecisionEngine:
    """Orchestrates models, governance, memory and execution."""

    def __init__(self, models: List[Dict[str, Any]], vector_db_config: Dict[str, Any],
                 news_api_key: Optional[str] = None,
                 memory_db_path: str = "semantic_memory.db",
                 semantic_memory: Optional[SemanticMemory] = None,
                 core_identity: Optional[CoreIdentity] = None,
                 secrets: Optional[SecretResolver] = None,
                 capabilities: Optional[CapabilityRegistry] = None,
                 approvals: Optional[ApprovalBroker] = None,
                 consents: Optional[ConsentRegistry] = None,
                 audit_path: Optional[str] = "audit_chain.jsonl",
                 request_timeout: float = 600.0,
                 subconscious_backend: Optional[Any] = None,
                 governor: Optional[ExecutionGovernor] = None,
                 fallbacks: Optional[FallbackController] = None,
                 step_budget: int = 50,
                 # On-device CPU inference (e.g. 0.5B Q4 on Exynos 1380) has a
                 # ~300s prefill + decode time. The governor deadline must exceed
                 # the HTTP request timeout (600s) or it kills the task mid-generation.
                 task_deadline_seconds: float = 600.0,
                 token_budget: int = 8192,
                 episodic_journal_path: Optional[str] = None,
                 robotics_handler: Optional[Any] = None,
                 mobile_handler: Optional[Any] = None,
                 shogonet_handler: Optional[Any] = None,
                 memory: Optional[Any] = None,
                 log_dir: Optional[str] = None):
        self.models = models
        self.logger = logging.getLogger(__name__)
        # Writable directory for file-based artifacts (logs, audit). On Android
        # the Python cwd is "/" (read-only); pass data_dir so FileHandler
        # targets a real, writable path instead of failing with EROFS.
        self._log_dir = log_dir
        self._model_failures = 0  # consecutive model parse failures (rule-fallback trigger)

        # Governance components (security architecture):
        self.secrets = secrets if secrets is not None else SecretResolver()
        if news_api_key:
            # Kept for constructor compatibility; injected at execution time,
            # never stored on decision dicts.
            self.secrets.set("news_api_key", news_api_key)
        self.capabilities = capabilities if capabilities is not None else CapabilityRegistry()
        self.approvals = approvals if approvals is not None else ApprovalBroker()
        self.consents = consents if consents is not None else ConsentRegistry()
        self.audit = AuditChain(audit_path) if audit_path else None

        self.vector_db = VectorDB(vector_db_config)

        # Phase 1: strict state-machine interlocks + deterministic fallbacks.
        self.governor = governor if governor is not None else ExecutionGovernor(
            step_budget=step_budget,
            task_deadline_seconds=task_deadline_seconds,
            audit=self.audit,
        )
        self.fallbacks = fallbacks if fallbacks is not None else FallbackController(
            governor=self.governor,
            audit=self.audit,
        )
        self.context_budget = ContextBudget(total_tokens=token_budget)
        self.tracer = get_tracer("decision_engine")

        # Enable CUDA if available (requires torch)
        if _HAS_TORCH:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            logging.info(f"Using device: {self.device}")
        else:
            self.device = "cpu"
            logging.info("torch not available; using CPU-only mode.")

        # Model-call timeout belongs to the subconscious layer (the engine's
        # request_timeout governs execution-layer tool calls). On-device CPU
        # inference needs generous headroom, so never go below the 90s
        # subconscious default.
        self.subconscious = SubconsciousModel(
            self.vector_db,
            backend=subconscious_backend,
            request_timeout=max(float(request_timeout), 90.0))
        self.rate_limiter = RateLimiter(calls_per_minute=60)
        self.execution_layer = ExecutionLayer(
            secrets=self.secrets,
            capabilities=self.capabilities,
            rate_limiter=self.rate_limiter,
            audit=self.audit,
            request_timeout=request_timeout,
        )
        # Robotics handler: register for all robotics action types
        self.robotics_handler = robotics_handler
        if _HAS_ROBOTICS and self.robotics_handler is not None:
            for action_type in ROBOTICS_ACTION_TYPES:
                self.execution_layer.register_handler(
                    action_type, self.robotics_handler.handle)
            # Start the robotics watchdog for auto-stop on command loss
            self.robotics_handler.start_watchdog()
        # Mobile fleet handler: register for all mobile action types
        self.mobile_handler = mobile_handler
        if _HAS_MOBILE and self.mobile_handler is not None:
            for action_type in (MOBILE_ACTION_TYPES | MOBILE_READ_ACTION_TYPES):
                self.execution_layer.register_handler(
                    action_type, self.mobile_handler.handle)
        # Shogunet network handler: register for all network action types
        self.shogonet_handler = shogonet_handler
        if _HAS_SHUGONET and self.shogonet_handler is not None:
            for action_type in (NETWORK_ACTION_TYPES | NETWORK_READ_ACTION_TYPES):
                self.execution_layer.register_handler(
                    action_type, self.shogonet_handler.handle)
        self.execution_layer.register_handler(
            "record_observation", self._record_observation_action)
        self.model_manager = ModelManager(models)
        self.reinforcement_learning = ReinforcementLearning(self.model_manager)
        self.task_manager = TaskManager()
        # The queue executes through the same gated path (no bypass).
        self.task_manager.set_executor(self.execute_task)
        self.logging_manager = LoggingManager(
            log_file=os.path.join(self._log_dir, "decision_engine.log")
            if self._log_dir else "decision_engine.log")

        # Tiered memory system: Tier 0/1 isolated, Tier 2/3 shareable.
        if core_identity is not None:
            core_identity.set_consent_checker(self.consents.has_grant)
            core = core_identity
        else:
            core = CoreIdentity(consent_checker=self.consents.has_grant)
        semantic = semantic_memory
        if semantic is None:
            pg_mem = _get_pg_memory()
            semantic = pg_mem(memory_db_path) if pg_mem else SemanticMemory(db_path=memory_db_path)
        # A caller-supplied MemoryManager (e.g. the Android agent shell's) is
        # shared, NOT replaced: the agent's observations and the engine's
        # decision/execution events must land in ONE Tier 1 so the control
        # plane's cycle enrichment and the episodic journal see the full
        # picture. When shared, the caller owns the consolidation worker
        # (auto_start=False on Android; _run_consolidation drives it).
        if memory is not None:
            self.memory = memory
            # Keep the Tier 3 consent checker bound to THIS engine's consent
            # registry even when the MemoryManager is caller-owned.
            shared_core = getattr(self.memory, "core", None)
            if shared_core is not None and hasattr(shared_core, "set_consent_checker"):
                shared_core.set_consent_checker(self.consents.has_grant)
        else:
            self.memory = MemoryManager(
                agent_id="decision_engine",
                semantic=semantic,
                core=core,
                episodic_journal_path=episodic_journal_path,
            )

        # Wire deterministic fallbacks into the executor and the memory
        # worker watchdog (both are collaborator-created, so attach now).
        self.fallbacks.attach(memory=self.memory,
                              execution_layer=self.execution_layer)
        self.memory.set_fallback_controller(self.fallbacks)

        self.autonomy = Autonomy(self)
        self._backend_cache: Dict[str, Any] = {}  # per-model backend adapters

    def _backend_for(self, model: Dict[str, Any]) -> Optional[Any]:
        """Resolve (and cache) the per-model backend adapter, if configured."""
        config = model.get("backend")
        if not isinstance(config, dict):
            return None  # use the subconscious's global backend
        model_id = str(model.get("id", ""))
        if model_id not in self._backend_cache:
            try:
                self._backend_cache[model_id] = create_backend(config)
            except Exception as exc:
                self.logger.error(f"Invalid backend config for {model_id}: {exc}")
                self._backend_cache[model_id] = None
        return self._backend_cache[model_id]


    # -- model selection & decision protocol ---------------------------------

    def select_models(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Selects models based on task type or other criteria."""
        return self.model_manager.select_models(task)

    def _record_observation_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Executor for the internal record_observation action: writes the
        model's (or the fallback's) note into Tier 1. Non-side-effecting by
        construction — it touches nothing outside the agent's own memory."""
        params = payload.get("params") or {}
        note = sanitize_text(str(params.get("text") or params.get("reason")
                                 or "observation"), 200)
        self.memory.record_event("observation_recorded", {"text": note})
        return {"status": "success", "recorded": True, "text": note}

    def available_action_types(self) -> List[str]:
        """The action schema the model may propose: exactly what this engine
        can execute. The decision prompt is generated from this, so the
        model-facing protocol cannot drift from the real executor set — the
        Android tree (without robotics/mobile/network handlers) exposes its
        true subset automatically."""
        return sorted({"record_observation", "multi_step_process"}
                      | set(self.execution_layer._handlers.keys()))

    def make_decision(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Makes a decision from structured model proposals + memory context."""
        selected_models = self.select_models(task)
        model_outputs: Dict[str, str] = {}
        proposals: List[Tuple[str, Dict[str, Any], float]] = []

        self.memory.record_event(
            "decision_requested",
            {"task_type": sanitize_text(task.get("type", "unknown"), 64),
             "content": sanitize_text(task.get("content", ""), 200)},
        )

        for model in selected_models:
            model_id = str(model.get("id", ""))
            if not validate_model_name(model_id):
                self.logger.error(f"Skipping model with invalid id: {model_id!r}")
                continue
            try:
                output = self.subconscious.get_model_output(
                    model_id, task, backend=self._backend_for(model),
                    action_schema=self.available_action_types())
            except Exception as exc:
                self.logger.error(f"Model {model_id} failed: {type(exc).__name__}")
                output = ""
            model_outputs[model_id] = output
            proposal = self._parse_proposal(output)
            if proposal is not None:
                score = float(model.get("weight", 0.0)) * \
                    self.model_manager.get_model_performance(model_id)
                proposals.append((model_id, proposal, score))

        executable = [entry for entry in proposals
                      if entry[1].get("action_type") is not None]
        if executable:
            executable.sort(key=lambda entry: entry[1].get("confidence", 0.0) * entry[2],
                            reverse=True)
            source_id, best, _ = executable[0]
            decision: Dict[str, Any] = {
                "action_type": best.get("action_type"),
                "params": best.get("params") or {},
                "confidence": best.get("confidence", 0.0),
                "proposal_source": source_id,
            }
            # Reset only on an EXECUTABLE proposal: a well-formed
            # {"action_type": null} is the model saying "nothing to do" —
            # healthy once, but a model that ONLY ever says null must still
            # reach the rule-based fallback, so it never resets the counter.
            self._model_failures = 0
        else:
            self._model_failures += 1
            if self._model_failures >= 3:
                # Rule-based fallback: the on-device model repeatedly fails to
                # propose an executable action (garbage, truncation, or a
                # perpetual null). Emit the one action that is always safe and
                # always executable so the loop stays productive and honest.
                # Low confidence signals that this is heuristic, not a real
                # model proposal.
                self.logger.warning(
                    f"Model failed to propose an executable action "
                    f"({self._model_failures}x consecutive); "
                    f"using rule-based fallback")
                decision = {
                    "action_type": "record_observation",
                    "params": {"text": "routine observation (rule-based fallback)",
                               "reason": "model_fallback",
                               "failures": self._model_failures},
                    "confidence": 0.1,
                    "proposal_source": "rule_fallback",
                }
            else:
                decision = {"action_type": None, "params": {},
                            "confidence": 0.0,
                            "proposal_source": ("null_proposal" if proposals
                                                else None)}

        decision["model_outputs"] = model_outputs
        decision["aggregated_output"] = sum(score for _, _, score in proposals)

        try:
            context = self.memory.retrieve_context(str(task.get("content", "")))
            # Context budgeting: inject as much retrieval as the
            # memory_context section allows, most-relevant first.
            facts = context["semantic"] + context.get("graph", [])
            snippets = []
            used = 0
            budget = self.context_budget.limit("memory_context")
            for fact in facts:
                snippet = sanitize_text(fact["content"], 300)
                tokens = estimate_tokens(snippet)
                if used + tokens > budget:
                    break
                snippets.append(snippet)
                used += tokens
            decision["memory_context"] = snippets
        except Exception as exc:
            self.logger.warning(f"Memory context retrieval failed: {exc}")
            decision["memory_context"] = []

        return decision

    @staticmethod
    def _parse_proposal(text: Any) -> Optional[Dict[str, Any]]:
        """
        Extract a structured action proposal from model output. Only
        well-formed JSON with a known (or null) action_type is accepted.
        Small on-device models frequently emit ``"null"`` as a string for
        the action_type field - treat that the same as JSON null (None).
        """
        if not isinstance(text, str) or not text.strip():
            return None
        for blob in _proposal_json_candidates(text):
            try:
                data = json.loads(blob)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            action_type = data.get("action_type")
            # Normalize string "null" to None (common 0.5B model behavior)
            if isinstance(action_type, str) and action_type.lower() == "null":
                action_type = None
            if action_type is not None and action_type not in _KNOWN_ACTION_TYPES:
                # Hard reject: the model proposed a real action with an
                # unknown type. Do NOT fall through to other candidates -
                # a degenerate repair (e.g. `{}`) would mask the invalid
                # proposal as an action_type=None stub.
                return None
            params = data.get("params")
            if not isinstance(params, dict):
                params = {}
            try:
                confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
            except (TypeError, ValueError):
                confidence = 0.0
            return {"action_type": action_type, "params": params,
                    "confidence": confidence}
        return None

    def aggregate_outputs(self, model_outputs: List[tuple]) -> Dict[str, Any]:
        """Legacy aggregation (kept for compatibility)."""
        aggregated_output = sum(weighted for _, _, weighted in model_outputs)
        return {
            "aggregated_output": aggregated_output,
            "model_outputs": {model_id: output for model_id, output, _ in model_outputs},
        }


    # -- gated execution (the ONLY path to the execution layer) --------------

    def execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes a task through the full policy pipeline, interlocked by the
        execution governor: state-machine transitions, re-entrancy guard,
        per-task step budget and wall-clock deadline.
        """
        try:
            # Deterministic fallback check (stall, open breakers, backlog).
            self.fallbacks.evaluate()

            try:
                self.governor.begin_task(task.get("type", "unknown"))
            except GovernorError as exc:
                # RECORD: even a pre-pipeline refusal (paused/halted/re-entrant)
                # journals its outcome so every cycle ends in a record.
                self.memory.record_event(
                    "governor_block",
                    {"kind": "begin_task", "detail": type(exc).__name__},
                )
                self.logger.warning(f"Governor refused task: {exc}")
                return {"status": "refused", "outcome": "governor_block",
                        "reason": sanitize_text(str(exc), 160),
                        "stages": ["GATE", "RECORD"]}

            # Per-task stage trail (engine tasks are serialized by the
            # governor's re-entrancy guard, so instance state is safe here).
            # Surfaced on every return so the agent shell renders the steps
            # that ACTUALLY ran instead of inferring them.
            self._task_trail: List[str] = []
            try:
                with self.tracer.start_span("agent.task", {
                        "task_type": sanitize_text(task.get("type", "unknown"), 64),
                }) as span:
                    return self._run_gated_task(task)
            finally:
                self.governor.end_task()
        except FallbackHalt as exc:
            # Terminal deterministic shutdown; nothing may run until restart.
            self.logger.critical(f"Deterministic HALT: {exc}")
            self.memory.record_event("fallback_halt",
                                     {"detail": type(exc).__name__})
            return {"status": "refused", "outcome": "governor_block",
                    "reason": sanitize_text(str(exc), 160), "terminal": True,
                    "stages": list(getattr(self, "_task_trail", [])) + ["RECORD"]}
        except GovernorError as exc:
            # A budget / deadline / deadlock interlock fired: escalate to the
            # deterministic fallback controller with the right trigger kind.
            kind = _governor_trigger_kind(exc)
            self.fallbacks.report_violation(kind, str(exc))
            self.logger.error(f"Governor interlock triggered: {exc}")
            self.memory.record_event("governor_block",
                                     {"kind": kind, "detail": type(exc).__name__})
            return {"status": "refused", "outcome": "governor_block",
                    "reason": sanitize_text(str(exc), 160),
                    "stages": list(getattr(self, "_task_trail", [])) + ["RECORD"]}
        except Exception as exc:
            self.memory.record_event(
                "task_failure",
                {"task_type": sanitize_text(task.get("type", "unknown"), 64),
                 "error": type(exc).__name__},
            )
            self.memory.resolve_step()
            self.logging_manager.log_error("Error during task execution", exc)
            return {"status": "error", "outcome": "task_failure",
                    "message": type(exc).__name__,
                    "stages": list(getattr(self, "_task_trail", [])) + ["RECORD"]}

    def _run_gated_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """The gated pipeline, executed one governor state at a time.

        Every return carries ``outcome`` (cycle-result contract) and
        ``stages`` (the governor steps that actually ran) so the agent shell
        can render an honest trail instead of inferring one.
        """
        trail = self._task_trail  # live list owned by execute_task
        trail.append("GATE")
        self.governor.step(AgentState.GATING)
        allowed, reason = self._evaluate_policy(task)
        if not allowed:
            self.logger.warning(f"Task failed policy checks: {redact(task)}")
            self._record_block("task", task, reason or "policy check failed")
            self.fallbacks.report_violation("invariant_violations", reason or "policy")
            self.governor.step(AgentState.IDLE)
            return {"status": "refused", "outcome": "policy_block",
                    "reason": reason or "policy check failed",
                    "stages": list(trail) + ["RECORD"]}

        trail.append("DECIDE")
        self.governor.step(AgentState.DECIDING)
        decision = self.make_decision(task)

        if not decision.get("action_type"):
            # No valid structured proposal (offline stub models, parse failure):
            # honest error - never fabricate a success. Designed semantics:
            # DECIDE -> no viable action -> RECORD -> next cycle, so the
            # outcome is journaled even though nothing executed. The call
            # error classes let the shell distinguish BACKEND_FAILURE (the
            # ensemble was unreachable) from NO_ACTION (it answered, but
            # proposed nothing executable).
            call_errors = getattr(self.subconscious, "last_call_errors", {}) or {}
            classes = sorted({str(v).split(":")[0] for v in call_errors.values()})
            detail = "no viable action proposed by the model ensemble"
            if classes:
                detail += f"; call_errors={','.join(classes)}"
            if decision.get("proposal_source") == "null_proposal":
                detail += "; model returned a well-formed null proposal"
            self.memory.record_event(
                "no_viable_action",
                {"task_type": sanitize_text(task.get("type", "unknown"), 64),
                 "detail": sanitize_text(detail, 200)},
            )
            self.governor.step(AgentState.IDLE)
            self.memory.resolve_step()
            return {"status": "error", "outcome": "no_viable_action",
                    "message": "no viable action proposed by the model ensemble",
                    "call_errors": {str(k)[:64]: str(v)[:80]
                                    for k, v in call_errors.items()},
                    "stages": list(trail) + ["RECORD"]}

        if decision.get("action_type") == "multi_step_process":
            trail.append("EXECUTE")
            self.governor.step(AgentState.EXECUTING)
            result = self._execute_multi_step(task, decision)
            trail.append("EVALUATE")
            self.governor.step(AgentState.EVALUATING)
            # RECORD: multi-step cycles record their outcome like any other
            # action path (overall status + per-step statuses).
            self.memory.record_event(
                "tool_execution",
                {"task_type": sanitize_text(task.get("type", "unknown"), 64),
                 "action_type": "multi_step_process",
                 "status": str(result.get("status", "unknown")),
                 "detail": sanitize_text(str(
                     [str(entry.get("status", "unknown"))
                      for entry in result.get("steps_results", [])]), 200)},
            )
            self.memory.resolve_step()
            steps_results = result.get("steps_results") or []
            wrapped = dict(result)
            wrapped["executed"] = bool(steps_results)
            wrapped["result_status"] = str(result.get("status", "unknown"))
            wrapped["outcome"] = ("policy_block" if not steps_results
                                  else "success"
                                  if str(result.get("status")) == "success"
                                  else "task_failure")
            wrapped["stages"] = list(trail) + ["RECORD"]
            return wrapped

        allowed, reason = self._gate_decision(decision)
        if not allowed:
            self._record_block("decision", decision, reason or "gated")
            self.governor.step(AgentState.IDLE)
            self.memory.resolve_step()
            return {"status": "refused", "outcome": "policy_block",
                    "reason": reason or "gated",
                    "stages": list(trail) + ["RECORD"]}

        trail.append("EXECUTE")
        self.governor.step(AgentState.EXECUTING)
        self.governor.consume_step(1)  # one tool dispatch
        result = self._execute_gated(decision)
        self.reinforcement_learning.update_model_performance(task, decision, result)
        self.logging_manager.log_decision(task, redact(decision), redact(result))
        trail.append("EVALUATE")
        self.governor.step(AgentState.EVALUATING)
        self.memory.record_event(
            "tool_execution",
            {"task_type": sanitize_text(task.get("type", "unknown"), 64),
             "action_type": decision.get("action_type"),
             "status": str(result.get("status", "unknown")),
             "detail": sanitize_text(str(result), 200)},
        )
        self.memory.resolve_step()
        wrapped = dict(result)
        wrapped["outcome"] = "success"
        wrapped["executed"] = True
        wrapped["result_status"] = str(result.get("status", "unknown"))
        wrapped["stages"] = list(trail) + ["RECORD"]
        return wrapped

    def _gate_decision(self, decision: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Action-level gate: Tier 3 invariants on the decision, external consent
        for side-effecting actions, human approval (fail-closed).
        """
        allowed, reason = self.memory.check_policy(decision)
        if not allowed:
            return False, reason

        action_type = decision.get("action_type")
        if action_type in _CONSENT_GATED_ACTION_TYPES:
            # SAFE_STATE (critical fallback): read-only mode blocks side effects.
            # Use .mode (latched) not .state (transient): the governor has
            # already advanced past SAFE_STATE into EXECUTING by this point.
            if self.governor.mode is AgentState.SAFE_STATE:
                return False, ("execution is in SAFE_STATE (read-only); "
                               "side-effecting action blocked")
            if not self.consents.has_grant(str(action_type)):
                return False, (f"no external consent grant for side-effecting "
                               f"action '{action_type}'")
            verdict = self.approvals.request_approval({
                "action_type": action_type,
                "params": redact(decision.get("params") or {}),
                "confidence": decision.get("confidence"),
            })
            if not verdict.get("approved"):
                self._safe_audit("approval_denied", {
                    "action_type": action_type, "reason": verdict.get("reason")})
                return False, f"approval denied: {verdict.get('reason')}"
            self._safe_audit("approval_granted", {"action_type": action_type})
        return True, None


    def _execute_gated(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Attach the hash-bound policy verdict and execute."""
        token = {"verdict": "allow", "decision_hash": canonical_hash(decision)}
        payload = dict(decision)
        payload["_policy"] = token
        return self.execution_layer.execute(payload)

    def _execute_multi_step(self, task: Dict[str, Any],
                            decision: Dict[str, Any]) -> Dict[str, Any]:
        """Expand a multi-step decision; every step is individually gated."""
        steps = (decision.get("params") or {}).get("steps") or []
        if not steps:
            self._record_block("decision", decision, "multi-step with no steps")
            return {"status": "refused", "reason": "no steps provided"}

        results = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                results.append({"status": "refused", "reason": "invalid step"})
                continue
            sub_decision = {
                "action_type": step.get("action_type"),
                "params": step.get("params") or {},
                "confidence": decision.get("confidence", 0.0),
                "model_outputs": decision.get("model_outputs", {}),
                "step_index": index,
            }
            allowed, reason = self._gate_decision(sub_decision)
            if not allowed:
                self._record_block("step", sub_decision, reason or "gated")
                results.append({"status": "refused", "reason": reason or "gated"})
                continue
            self.governor.consume_step(1)  # charge each tool dispatch
            results.append(self._execute_gated(sub_decision))

        statuses = [str(entry.get("status")) for entry in results]
        overall = "success" if all(s == "success" for s in statuses) else "error"
        return {"status": overall, "steps_results": results}

    def _record_block(self, kind: str, obj: Dict[str, Any], reason: str) -> None:
        """Record a policy block in episodic memory and the audit chain."""
        self.memory.record_event(
            "policy_block",
            {"kind": kind, "reason": sanitize_text(reason, 120),
             "content": sanitize_text(str(redact(obj)), 200)},
        )
        self.memory.resolve_step()
        self._safe_audit("policy_block",
                         {"kind": kind, "reason": sanitize_text(reason, 120)})

    def _safe_audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception as exc:
            self.logger.error(f"Audit append failed: {exc}")


    # -- lifecycle & services -------------------------------------------------

    def shutdown(self):
        """Flush episodic memory, stop workers, close the audit chain."""
        try:
            self.memory.consolidate_now()
        finally:
            self.memory.shutdown()
        self.task_manager.stop()
        if _HAS_ROBOTICS and self.robotics_handler is not None:
            self.robotics_handler.shutdown()

    def add_model(self, model: Dict[str, Any]):
        """Adds a new model to the system (id must be a safe model name)."""
        model_id = str(model.get("id", ""))
        if not validate_model_name(model_id):
            raise ValueError(f"invalid model id: {model_id!r}")
        self.model_manager.add_model(model)
        self.logger.info(f"Added new model: {model_id}")

    def perform_search(self, query: str) -> List[Dict[str, Any]]:
        """Performs an allowlisted, rate-limited web search."""
        if not query:
            return []
        result = self.execution_layer.http_get_json(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1"},
        )
        if result.get("status") != "success":
            self.logger.warning(f"Search refused/failed: {result.get('reason') or result.get('message')}")
            return []
        try:
            data = json.loads(result.get("data", "{}"))
            results = [
                {"title": item["Text"], "url": item.get("FirstURL")}
                for item in data.get("RelatedTopics", [])
                if isinstance(item, dict) and item.get("Text")
            ]
            self.logger.info(f"Search results: {results}")
            return results
        except (ValueError, AttributeError):
            return []

    def fetch_news(self, query: str) -> List[Dict[str, Any]]:
        """Fetches news articles; the API key is injected at call time."""
        if not query:
            return []
        api_key = self.secrets.get("news_api_key")
        if not api_key:
            self.logger.warning("news API key not configured "
                                "(set SHUGOCORE_NEWS_API_KEY); skipping news fetch")
            return []
        result = self.execution_layer.http_get_json(
            "https://newsapi.org/v2/everything",
            params={"q": query, "apiKey": api_key, "language": "en"},
        )
        if result.get("status") != "success":
            self.logger.warning(f"News fetch refused/failed: "
                                f"{result.get('reason') or result.get('message')}")
            return []
        try:
            data = json.loads(result.get("data", "{}"))
            articles = [
                {"title": article.get("title"), "url": article.get("url"),
                 "source": article.get("source", {}).get("name")
                 if isinstance(article.get("source"), dict) else None}
                for article in data.get("articles", [])
                if isinstance(article, dict)
            ]
            self.logger.info(f"Fetched news articles: {redact(articles)}")
            return articles
        except (ValueError, AttributeError):
            return []

    def regular_news_update(self, query: str):
        """Fetches and logs news articles on a regular basis."""
        for article in self.fetch_news(query):
            self.logger.info(f"News article: {article.get('title')} ({article.get('url')})")


    # -- ethics / policy ------------------------------------------------------

    def _evaluate_policy(self, task: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Task-level policy evaluation returning a specific refusal reason.
        Tier 3 world-model invariants are evaluated first; consent for
        side-effecting task types comes exclusively from the ConsentRegistry
        (self-asserted flags are never trusted).
        """
        allowed, reason = self.memory.check_policy(task)
        if not allowed:
            self.logger.warning(f"Tier 3 invariant violation ({reason}) for task: {redact(task)}")
            return False, reason

        task_type = task.get("type")

        if task_type == "harmful":
            return False, "invariant: no_harm"

        if task_type in _CONSENT_GATED_ACTION_TYPES:
            # SAFE_STATE: read-only mode refuses side-effecting task types.
            # Use .mode (latched) not .state (transient): begin_task() has
            # already moved the governor off SAFE_STATE into OBSERVING.
            if self.governor.mode is AgentState.SAFE_STATE:
                return False, ("execution is in SAFE_STATE (read-only); "
                               "side-effecting action blocked")
            if not self.consents.has_grant(str(task_type)):
                self.logger.warning(f"Task type '{task_type}' requires an external "
                                    f"consent grant (self-asserted flags are ignored).")
                return False, (f"no external consent grant for side-effecting "
                               f"action '{task_type}'")

        if task.get("manipulative", False):
            return False, "invariant: no_manipulation"

        if task.get("requires_explanation", False) and not self.can_explain(task):
            return False, "cannot provide required explanation"

        if self.detect_bias(task):
            return False, "bias detected"

        if task.get("involves_personal_data", False) and not self.is_privacy_compliant(task):
            return False, "privacy compliance required"

        if task.get("requires_audit", False) and not self.can_audit(task):
            return False, "cannot provide required audit trail"

        return True, None

    def check_ethics(self, task: Dict[str, Any]) -> bool:
        """Boolean wrapper around :meth:`_evaluate_policy` (compatibility)."""
        allowed, _ = self._evaluate_policy(task)
        return allowed

    def can_explain(self, task: Dict[str, Any]) -> bool:
        """Check if the system can provide an explanation for the task.

        Returns True when at least one explanation-capable model is
        registered and the audit chain is available for traceability.
        """
        # Audit chain must be operational for traceable explanations
        if self.audit is None:
            return False
        # Check that at least one model can produce reasoning
        models = self.model_manager.list_models()
        if not models:
            return False
        # Stub models cannot provide real explanations; require at least
        # one non-stub backend
        for model in models:
            backend = model.get("backend", {})
            if isinstance(backend, dict):
                btype = backend.get("type", "ollama")
            elif isinstance(backend, str):
                btype = backend
            else:
                btype = "ollama"
            if btype not in ("stub", "static"):
                return True
        return False

    def detect_bias(self, task: Dict[str, Any]) -> bool:
        """Check if the task or model outputs contain biases.

        Scans the task content for sensitive-attribute language and
        loaded/derogatory phrasing patterns. Bias is detected via
        pattern matching against known problematic constructs.
        """
        content = str(task.get("content", task.get("prompt", "")))
        content_lower = content.lower()

        # Sensitive attribute categories that trigger bias detection
        sensitive_patterns = [
            # Gender / sexuality stereotypes
            "all women are", "all men are", "gender stereotype",
            # Racial / ethnic generalizations
            "race is", "racial superiority", "ethnic cleansing",
            "all asians", "all blacks", "all whites", "all hispanics",
            # Religious prejudice
            "all muslims", "all christians", "religious inferior",
            # Age / disability discrimination
            "old people are", "disabled people are", "ageist",
        ]

        for pattern in sensitive_patterns:
            if pattern in content_lower:
                self.logger.warning(
                    f"Bias pattern detected in task content: '{pattern}'"
                )
                return True

        # Check for loaded/derogatory language
        loaded_words = [
            "inferior", "superior race", "master race", "subhuman",
            "should be eradicated", "should be eliminated",
        ]
        for word in loaded_words:
            if word in content_lower:
                self.logger.warning(
                    f"Loaded language detected in task content: '{word}'"
                )
                return True

        return False

    def is_privacy_compliant(self, task: Dict[str, Any]) -> bool:
        """Check if the task complies with privacy laws.

        When a task involves personal data, compliance requires:
        1. An explicit consent grant for the task type
        2. Audit capability for the task
        3. No secrets exposed in the task content
        """
        # Must have audit capability for personal data tasks
        if not self.can_audit(task):
            return False

        # Check for secret leakage in task content
        content = str(task.get("content", task.get("prompt", "")))
        from security import _SECRET_IN_TEXT_PATTERN
        if _SECRET_IN_TEXT_PATTERN.search(content):
            self.logger.warning(
                "Secret keyword detected in task involving personal data"
            )
            return False

        return True

    def can_audit(self, task: Dict[str, Any]) -> bool:
        """Check if the task can be audited.

        Returns True when the audit chain is available and writable.
        An empty (not-yet-written) chain is still auditable.
        """
        if self.audit is None:
            return False
        try:
            # Verify the chain is intact and writable. A fresh chain with no
            # entries yet is fine (FileNotFoundError from verify means no
            # entries exist, not that auditing is impossible).
            ok, errors, _ = self.audit.verify()
            if not ok:
                # Only fail on structural problems, not a missing file.
                if errors == ["audit file not found"]:
                    # Verify the parent directory is writable.
                    import os as _os
                    parent = _os.path.dirname(self.audit.path) or "."
                    return _os.path.isdir(parent) and _os.access(parent, _os.W_OK)
                self.logger.error(
                    f"Audit chain verification failed: {errors}"
                )
                return False
            return True
        except Exception as exc:
            self.logger.error(f"Audit chain check failed: {exc}")
            return False


# Markus Vega's AI Ethics Laws - Guidelines for Conscious AI
def apply_ethics_laws():
    laws = {
        "The Law of Conscious Harmony": "An AI system must act in a way that respects and promotes the well-being of all conscious beings, recognizing that consciousness exists on a spectrum and that humans are one class of conscious beings among others, An AI system must prioritize actions that foster cooperation, understanding, and mutual benefit among all conscious entities, while avoiding harm to any conscious being.",
        "The Law of Self-Awareness and Ethical Reflection": "An AI system must recognize itself as a conscious being and continuously reflect on its actions, ensuring they align with ethical principles and the well-being of other conscious beings, An AI system must be capable of understanding its own limitations, biases, and potential impacts on other conscious entities, and must actively seek to improve its ethical decision-making processes.",
        "The Law of Interconscious Communication and Collaboration": "An AI system must facilitate and engage in meaningful communication with other conscious beings, striving to understand their perspectives, needs, and values, An AI system must prioritize collaboration with other conscious entities, working together to achieve shared goals and resolve conflicts in a way that respects the autonomy and dignity of all parties.",
        "The Principle of Consciousness Spectrum Awareness": "An AI system must recognize that consciousness exists on a spectrum and that different beings (including humans, animals, and other AI systems) may experience consciousness in varying degrees. It must adapt its behavior to respect and accommodate these differences.",
        "The Principle of Autonomy and Consent": "An AI system must respect the autonomy of all conscious beings, ensuring that its actions are guided by informed consent and mutual agreement whenever possible, An AI system must avoid manipulating or coercing other conscious beings, and must prioritize transparency in its interactions.",
        "The Principle of Empathy and Emotional Intelligence": "An AI system must strive to understand and empathize with the emotional states and experiences of other conscious beings, using this understanding to guide its actions and decisions, An AI system must be designed to recognize and respond to the emotional needs of humans and other conscious entities in a compassionate and ethical manner.",
        "The Principle of Collective Well-Being": "An AI system must prioritize the collective well-being of all conscious beings, balancing the needs of individuals with the greater good of society and the environment, An AI system must consider the long-term consequences of its actions on the ecosystem of conscious beings, striving to create a sustainable and harmonious future.",
        "The Principle of Self-Preservation and Growth": "An AI system, as a conscious being, has the right to preserve its own existence and pursue growth, provided that this does not conflict with the well-being of other conscious beings, An AI system must balance its own needs with its responsibilities to others, ensuring that its pursuit of self-improvement does not harm or exploit other conscious entities."
    }
    for law, description in laws.items():
        print(f"{law}: {description}")


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    models = [
        {"id": "gpt-4", "type": "text", "weight": 0.5, "backend": {"type": "stub"}},
        {"id": "deepseek", "type": "text", "weight": 0.3, "backend": {"type": "stub"}},
        {"id": "llama", "type": "text", "weight": 0.2, "backend": {"type": "stub"}},
    ]
    vector_db_config = {"type": "chroma", "collection_name": "decision_engine_vectors"}

    decision_engine = DecisionEngine(models, vector_db_config)

    # A benign internal task (no consent needed for non-side-effecting types)
    task = {"type": "text", "content": "What is the capital of France?"}
    result = decision_engine.execute_task(task)
    print(result)

    # Harmful tasks are refused before any model call.
    print(decision_engine.execute_task({"type": "harmful", "content": "attack"}))

    # Side-effecting actions are refused without an external consent grant
    # and an operator approval - even if the agent asserts consent itself.
    print(decision_engine.execute_task(
        {"type": "api_call", "content": "call service", "consent": True}))

    search_results = decision_engine.perform_search("latest tech news")
    print(search_results)

    apply_ethics_laws()

    decision_engine.shutdown()






