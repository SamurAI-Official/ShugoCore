"""
ShugoCore subconscious model
============================

Generates model output through pluggable backends (see ``model_backends``):

- The legacy per-call ``ollama`` subprocess is gone: the Ollama HTTP API on
  loopback is used instead (no argument-injection surface, enforced timeouts,
  cached model listing).
- Model names are validated (no leading dashes, bounded length) before any
  use.
- Prompts ask for a structured JSON action proposal, which the decision
  engine parses and aggregates (highest confidence-weighted proposal wins).

Backwards-compatible helpers (``get_available_models``, ``call_ollama_model``,
``weight_models_based_on_success``, ``log_model_success``) are preserved.
"""

import json  # noqa: F401 - kept for callers that import it from this module
import logging
import inspect
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional


from model_backends import BaseBackend, create_backend, validate_model_name
from security import canonical_json, sanitize_text
from telemetry import get_tracer
from vector_db import VectorDB

logger = logging.getLogger(__name__)

_DECISION_PROMPT = (
    "You are the decision module of an autonomous agent. Respond ONLY with a "
    "single-line JSON object with keys: \"action_type\" (one of api_call, "
    "database_update, hardware_interaction, news_api, search_api, "
    "multi_step_process, or null), \"params\" (object), \"confidence\" "
    "(number between 0.0 and 1.0), and \"text\" (short explanation). "
    "Keep the entire JSON object on one line with no line breaks, and do not "
    "add any text outside the JSON.\n"
    "Task: {task_json}"
)


# GBNF template constraining a decision proposal to the engine's own schema.
# 'action' is substituted with the real executor set, so grammar-constrained
# decoding cannot propose an action the engine does not support. A tiny model
# then physically cannot emit the loose dialects ("action_type: speak" with no
# braces, "name: {json}") that used to fail _parse_proposal and force the
# rule-based fallback forever.
_DECISION_GRAMMAR_TEMPLATE = r"""
root       ::= "{" ws "\"action_type\"" ws ":" ws action ws "," ws "\"params\"" ws ":" ws params ws "," ws "\"confidence\"" ws ":" ws number ws "," ws "\"text\"" ws ":" ws string ws "}"
action     ::= %s
params     ::= "{" ws (string ws ":" ws value (ws "," ws string ws ":" ws value)*)? ws "}" ws
value      ::= object | array | string | number | ("true" | "false" | "null") ws
object     ::= "{" ws (string ws ":" ws value (ws "," ws string ws ":" ws value)*)? ws "}" ws
array      ::= "[" ws (value (ws "," ws value)*)? ws "]" ws
string     ::= "\"" ( [^"\\\x7F\x00-\x1F] | "\\" (["\\bfnrt] | "u" [0-9a-fA-F]{4}) )* "\"" ws
number     ::= ("-"? ([0-9] | [1-9] [0-9]{0,15})) ("." [0-9]+)? ([eE] [-+]? [0-9] [1-9]{0,15})? ws
ws         ::= | " " | "\n" [ \t]{0,20}
"""


def build_decision_grammar(action_schema) -> str:
    """GBNF that forces a parseable decision proposal for ``action_schema``.

    Returns "" when no action types are supplied (callers then decode
    unconstrained, exactly as before). The grammar pins the key order the
    decision prompt already asks for and restricts ``action_type`` to the
    engine's real executor set plus ``null``.
    """
    types = sorted({str(t).strip() for t in (action_schema or [])
                    if str(t).strip() and str(t).strip().lower() != "null"})
    if not types:
        return ""
    alternatives = " | ".join('"\\"%s\\""' % t for t in types + ["null"])
    return (_DECISION_GRAMMAR_TEMPLATE.lstrip("\n") % alternatives)


def _build_decision_prompt(task_json: str, action_schema) -> str:
    """Generated decision prompt: the action-type list comes from the
    engine's real executor set (available_action_types), so the model-facing
    protocol can never drift from what policy/execution actually support."""
    types = ", ".join(sorted(action_schema)) + ", or null"
    names = set(action_schema)
    speech_line = ""
    if "speak" in names or "ask_user" in names:
        speech_line = (
            "You are Shugo, a calm and friendly local assistant; you speak "
            "in first person. If you choose 'speak' or 'ask_user', put your "
            "words in params and say exactly ONE warm, complete sentence - "
            "never a fragment, never a list. ")
    # v1.20: extract conversation summary from task context if present
    conv_line = ""
    try:
        import json as _json
        task_dict = _json.loads(task_json)
        ctx = task_dict.get("context", {})
        if ctx.get("conversation_summary"):
            conv_line = "Recent conversation:\n" + sanitize_text(ctx["conversation_summary"], 2000) + "\n\n"
        if ctx.get("human", {}).get("user_context", {}).get("entity_facts"):
            facts = ctx["human"]["user_context"]["entity_facts"]
            conv_line += "Relevant facts from memory:\n"
            for f in facts[:3]:
                conv_line += "  - " + sanitize_text(str(f.get("content", "")), 200)[:120] + "\n"
            conv_line += "\n"
    except Exception:
        pass
    return (
        "You are the decision module of an autonomous agent. "
        + speech_line + conv_line + "Respond ONLY with a "
        "single-line JSON object with keys: \"action_type\" (one of "
        f"{types}), \"params\" (object), \"confidence\" "
        "(number between 0.0 and 1.0), and \"text\" (short explanation). "
        "\"record_observation\" is always safe and always available: it records "
        "a short observation (put your note in params.text). Prefer it for "
        "routine self-maintenance when no other action is clearly required. "
        "Side-effecting actions (network send/query/sync, device or hardware "
        "control) need operator approval, so choose them only when the task "
        "clearly asks for them. Choose null only "
        "if there is truly nothing worth recording. Keep the entire JSON "
        "object on one line with no line breaks, and do not add any text "
        "outside the JSON.\n"
        "Task: {task_json}"
    )


class SubconsciousModel:
    """Generates model outputs via pluggable backends (Ollama HTTP default)."""

    def __init__(self, vector_db: Optional[VectorDB] = None,
                 backend: Optional[BaseBackend] = None,
                 backend_config: Optional[Dict[str, Any]] = None,
                 request_timeout: float = 600.0,
                 model_list_cache_seconds: float = 60.0):
        self.vector_db = vector_db
        self.backend = backend if backend is not None else create_backend(backend_config)
        self.request_timeout = max(1.0, float(request_timeout))
        self.model_list_cache_seconds = max(0.0, float(model_list_cache_seconds))
        self.model_success_history: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"successes": 0, "failures": 0}
        )
        # Failure class of the last call per model (cycle diagnostics: the
        # engine surfaces these in no_viable_action results so the shell can
        # distinguish BACKEND_FAILURE from NO_ACTION).
        self.last_call_errors: Dict[str, str] = {}
        self._models_cache: Optional[List[str]] = None
        self._models_cache_at = 0.0
        self._models_lock = threading.Lock()
        self._history_lock = threading.Lock()
        # type(backend) -> whether backend.generate accepts `grammar`. Cached
        # so the capability probe costs nothing after the first call.
        self._grammar_support_cache: Dict[type, bool] = {}
        self.logger = logging.getLogger(__name__)

    def _backend_accepts_grammar(self, backend: BaseBackend) -> bool:
        """Whether ``backend.generate`` accepts the ``grammar`` kwarg.

        Older backends and lightweight test doubles may predate the
        grammar-constrained calling convention; probing (once per class) keeps
        them working instead of raising TypeError on every decision.
        """
        key = type(backend)
        cached = self._grammar_support_cache.get(key)
        if cached is not None:
            return cached
        supported = False
        try:
            params = inspect.signature(backend.generate).parameters
            supported = "grammar" in params or any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in params.values())
        except (TypeError, ValueError):
            supported = False
        self._grammar_support_cache[key] = supported
        return supported

    # -- backend plumbing ----------------------------------------------------

    def get_available_models(self) -> List[str]:
        """Cached backend model listing (previously shelled out every call)."""
        with self._models_lock:
            now = time.monotonic()
            if (self._models_cache is not None
                    and now - self._models_cache_at < self.model_list_cache_seconds):
                return list(self._models_cache)
        try:
            models = list(self.backend.list_models())
        except Exception as exc:
            logger.warning(f"Could not list backend models: {type(exc).__name__}")
            models = []
        with self._models_lock:
            self._models_cache = models
            self._models_cache_at = time.monotonic()
        return list(models)

    def note_call_error(self, model_name: str, error_class: str) -> None:
        """Record the failure class of a model call (best-effort, bounded)."""
        try:
            if len(self.last_call_errors) > 64:
                self.last_call_errors.clear()
            self.last_call_errors[str(model_name)[:64]] = str(error_class)[:80]
        except Exception:
            pass

    def call_ollama_model(self, model_name: str, input_data: str) -> str:
        """Backwards-compatible direct model call (HTTP, validated, timeout)."""
        if not validate_model_name(model_name):
            logger.error(f"Rejected invalid model name: {model_name!r}")
            return ""
        try:
            return str(self.backend.generate(model_name, str(input_data),
                                             timeout=self.request_timeout))
        except Exception as exc:
            logger.error(f"Model {model_name} call failed: {type(exc).__name__}")
            return ""

    def get_model_output(self, model_name: str, input_data: Any,
                         backend: Optional[BaseBackend] = None,
                         action_schema: Optional[List[str]] = None) -> str:
        """
        Query a model with a structured-decision prompt. Returns the raw text
        output ('' on failure), parsed by the decision engine into a proposal.
        ``backend`` overrides the global backend (per-model adapters).
        """
        if not validate_model_name(model_name):
            logger.error(f"Rejected invalid model name: {model_name!r}")
            self.note_call_error(model_name, "invalid_model")
            return ""
        backend = backend or self.backend
        available = self.get_available_models()
        if (backend is self.backend and available
                and model_name not in available):
            logger.error(f"Model {model_name} is not available.")
            self.note_call_error(model_name, "model_unavailable")
            return ""
        task_payload = (input_data if isinstance(input_data, dict)
                        else {"content": str(input_data)})
        task_json = canonical_json(sanitize_text(canonical_json(task_payload), 2000))
        if action_schema:
            prompt = _build_decision_prompt(task_json, action_schema)
        else:
            prompt = _DECISION_PROMPT.format(task_json=task_json)
        if not validate_model_name(model_name):
            logger.error(f"Rejected invalid model name: {model_name!r}")
            return ""
        with get_tracer("subconscious").start_span(
                "backend.generate", {"model": sanitize_text(model_name, 64)}) as span:
            try:
                # Grammar-constrained decoding for the decision path only:
                # conversational output (get_conversational_output) stays free
                # text. Backends without GBNF map it to their JSON mode; the
                # Android server forwards it to llama.cpp's grammar sampler.
                grammar = build_decision_grammar(action_schema)
                if grammar and self._backend_accepts_grammar(backend):
                    output = str(backend.generate(model_name, prompt,
                                                  timeout=self.request_timeout,
                                                  grammar=grammar))
                else:
                    output = str(backend.generate(model_name, prompt,
                                                  timeout=self.request_timeout))
                span.set_attribute("chars", len(output))
                self.last_call_errors.pop(str(model_name)[:64], None)
                return output
            except Exception as exc:
                span.set_attribute("error", type(exc).__name__)
                logger.error(f"Model {model_name} call failed: {type(exc).__name__}")
                self.note_call_error(model_name,
                                     f"transport_error: {type(exc).__name__}")
                return ""



    def get_conversational_output(self, model_name: str,
                                  prompt: str,
                                  backend: Optional[BaseBackend] = None) -> str:
        """Query a model with a conversational prompt (personality-driven).

        Unlike get_model_output(), this does NOT build a tool-use decision
        prompt — the caller provides the full conversational prompt (from
        prompts.builder.build_conversational_prompt). Returns raw model
        output ('' on failure). The decision engine parses the speak action.
        """
        if not validate_model_name(model_name):
            logger.error(f"Rejected invalid model name: {model_name!r}")
            return ""
        backend = backend or self.backend
        available = self.get_available_models()
        if (backend is self.backend and available
                and model_name not in available):
            logger.error(f"Model {model_name} is not available.")
            return ""
        with get_tracer("subconscious").start_span(
                "backend.generate_conversation",
                {"model": sanitize_text(model_name, 64)}) as span:
            try:
                output = str(backend.generate(model_name, prompt,
                                              timeout=self.request_timeout))
                span.set_attribute("chars", len(output))
                self.last_call_errors.pop(str(model_name)[:64], None)
                return output
            except Exception as exc:
                span.set_attribute("error", type(exc).__name__)
                logger.error(
                    f"Conversational model {model_name} call failed: "
                    f"{type(exc).__name__}")
                self.note_call_error(
                    model_name, f"transport_error: {type(exc).__name__}")
                return ""

    # -- success bookkeeping ---------------------------------------------------

    def log_model_success(self, model_id: str, success: bool) -> None:
        """Log the success or failure of a model (thread-safe)."""
        with self._history_lock:
            key = "successes" if success else "failures"
            self.model_success_history[model_id][key] += 1
        self.logger.info(f"Model {model_id} success: {success}")

    def weight_models_based_on_success(self, models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Re-weight models by historical success rate (input not mutated)."""
        with self._history_lock:
            rates = {
                model_id: (entry["successes"] /
                           (entry["successes"] + entry["failures"]))
                if (entry["successes"] + entry["failures"]) > 0 else 0.0
                for model_id, entry in self.model_success_history.items()
            }

        try:
            import torch  # optional: tensor weighting when available
            values = [rates.get(str(model.get('id')), 0.0) for model in models]
            success_rates = torch.tensor(values, dtype=torch.float32).tolist()
        except ImportError:
            success_rates = [rates.get(str(model.get('id')), 0.0) for model in models]

        weighted = []
        for model, rate in zip(models, success_rates):
            entry = dict(model)
            entry['weight'] = rate
            weighted.append(entry)
        return weighted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    subconscious = SubconsciousModel(backend_config={"type": "stub"})
    print(subconscious.get_model_output("stub-model", {"type": "test", "content": "hello"}))

