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
from logging_manager import LoggingManager
from memory_system import CoreIdentity, MemoryManager, SemanticMemory
from model_backends import create_backend, validate_model_name
from model_manager import ModelManager
from policy import (
    SIDE_EFFECTING_ACTION_TYPES,
    ApprovalBroker,
    CapabilityRegistry,
    ConsentRegistry,
)
from reinforcement_learning import ReinforcementLearning
from security import (
    RateLimiter,
    SecretResolver,
    canonical_hash,
    redact,
    sanitize_text,
)
from subconscious import SubconsciousModel
from task_manager import TaskManager
from vector_db import VectorDB

logger = logging.getLogger(__name__)

_PROPOSAL_JSON_PATTERN = re.compile(r"\{.*\}", re.DOTALL)

_KNOWN_ACTION_TYPES = {
    "api_call", "database_update", "hardware_interaction",
    "news_api", "search_api", "multi_step_process",
}


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
                 request_timeout: float = 10.0,
                 subconscious_backend: Optional[Any] = None):
        self.models = models
        self.logger = logging.getLogger(__name__)

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

        # Enable CUDA if available (requires torch)
        if _HAS_TORCH:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            logging.info(f"Using device: {self.device}")
        else:
            self.device = "cpu"
            logging.info("torch not available; using CPU-only mode.")

        self.subconscious = SubconsciousModel(self.vector_db,
                                              backend=subconscious_backend)
        self.rate_limiter = RateLimiter(calls_per_minute=60)
        self.execution_layer = ExecutionLayer(
            secrets=self.secrets,
            capabilities=self.capabilities,
            rate_limiter=self.rate_limiter,
            audit=self.audit,
            request_timeout=request_timeout,
        )
        self.model_manager = ModelManager(models)
        self.reinforcement_learning = ReinforcementLearning(self.model_manager)
        self.task_manager = TaskManager()
        # The queue executes through the same gated path (no bypass).
        self.task_manager.set_executor(self.execute_task)
        self.logging_manager = LoggingManager()

        # Tiered memory system: Tier 0/1 isolated, Tier 2/3 shareable.
        if core_identity is not None:
            core_identity.set_consent_checker(self.consents.has_grant)
            core = core_identity
        else:
            core = CoreIdentity(consent_checker=self.consents.has_grant)
        self.memory = MemoryManager(
            agent_id="decision_engine",
            semantic=semantic_memory if semantic_memory is not None
            else SemanticMemory(db_path=memory_db_path),
            core=core,
        )

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
                    model_id, task, backend=self._backend_for(model))
            except Exception as exc:
                self.logger.error(f"Model {model_id} failed: {type(exc).__name__}")
                output = ""
            model_outputs[model_id] = output
            proposal = self._parse_proposal(output)
            if proposal is not None:
                score = float(model.get("weight", 0.0)) * \
                    self.model_manager.get_model_performance(model_id)
                proposals.append((model_id, proposal, score))

        if proposals:
            proposals.sort(key=lambda entry: entry[1].get("confidence", 0.0) * entry[2],
                           reverse=True)
            source_id, best, _ = proposals[0]
            decision: Dict[str, Any] = {
                "action_type": best.get("action_type"),
                "params": best.get("params") or {},
                "confidence": best.get("confidence", 0.0),
                "proposal_source": source_id,
            }
        else:
            decision = {"action_type": None, "params": {},
                        "confidence": 0.0, "proposal_source": None}

        decision["model_outputs"] = model_outputs
        decision["aggregated_output"] = sum(score for _, _, score in proposals)

        try:
            context = self.memory.retrieve_context(str(task.get("content", "")))
            decision["memory_context"] = [sanitize_text(fact["content"], 300)
                                          for fact in context["semantic"]]
        except Exception as exc:
            self.logger.warning(f"Memory context retrieval failed: {exc}")
            decision["memory_context"] = []

        return decision

    @staticmethod
    def _parse_proposal(text: Any) -> Optional[Dict[str, Any]]:
        """
        Extract a structured action proposal from model output. Only
        well-formed JSON with a known (or null) action_type is accepted.
        """
        if not isinstance(text, str) or not text.strip():
            return None
        match = _PROPOSAL_JSON_PATTERN.search(text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        action_type = data.get("action_type")
        if action_type is not None and action_type not in _KNOWN_ACTION_TYPES:
            return None
        params = data.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        return {"action_type": action_type, "params": params, "confidence": confidence}

    def aggregate_outputs(self, model_outputs: List[tuple]) -> Dict[str, Any]:
        """Legacy aggregation (kept for compatibility)."""
        aggregated_output = sum(weighted for _, _, weighted in model_outputs)
        return {
            "aggregated_output": aggregated_output,
            "model_outputs": {model_id: output for model_id, output, _ in model_outputs},
        }


    # -- gated execution (the ONLY path to the execution layer) --------------

    def execute_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Executes a task through the full policy pipeline."""
        try:
            allowed, reason = self._evaluate_policy(task)
            if not allowed:
                self.logger.warning(f"Task failed policy checks: {redact(task)}")
                self._record_block("task", task, reason or "policy check failed")
                return {"status": "refused", "reason": reason or "policy check failed"}

            decision = self.make_decision(task)

            if decision.get("action_type") == "multi_step_process":
                return self._execute_multi_step(task, decision)

            allowed, reason = self._gate_decision(decision)
            if not allowed:
                self._record_block("decision", decision, reason or "gated")
                return {"status": "refused", "reason": reason or "gated"}

            result = self._execute_gated(decision)
            self.reinforcement_learning.update_model_performance(task, decision, result)
            self.logging_manager.log_decision(task, redact(decision), redact(result))
            self.memory.record_event(
                "tool_execution",
                {"task_type": sanitize_text(task.get("type", "unknown"), 64),
                 "action_type": decision.get("action_type"),
                 "status": str(result.get("status", "unknown")),
                 "detail": sanitize_text(str(result), 200)},
            )
            self.memory.resolve_step()
            return result
        except Exception as exc:
            self.memory.record_event(
                "task_failure",
                {"task_type": sanitize_text(task.get("type", "unknown"), 64),
                 "error": type(exc).__name__},
            )
            self.memory.resolve_step()
            self.logging_manager.log_error("Error during task execution", exc)
            return {"status": "error", "message": type(exc).__name__}

    def _gate_decision(self, decision: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Action-level gate: Tier 3 invariants on the decision, external consent
        for side-effecting actions, human approval (fail-closed).
        """
        allowed, reason = self.memory.check_policy(decision)
        if not allowed:
            return False, reason

        action_type = decision.get("action_type")
        if action_type in SIDE_EFFECTING_ACTION_TYPES:
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

        if task_type in SIDE_EFFECTING_ACTION_TYPES and \
                not self.consents.has_grant(str(task_type)):
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
        """Check if the system can provide an explanation for the task."""
        return True  # Placeholder

    def detect_bias(self, task: Dict[str, Any]) -> bool:
        """Check if the task or model outputs contain biases."""
        return False  # Placeholder

    def is_privacy_compliant(self, task: Dict[str, Any]) -> bool:
        """Check if the task complies with privacy laws."""
        return True  # Placeholder

    def can_audit(self, task: Dict[str, Any]) -> bool:
        """Check if the task can be audited."""
        return True  # Placeholder


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






