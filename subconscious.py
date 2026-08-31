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
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from model_backends import BaseBackend, create_backend, validate_model_name
from security import canonical_json, sanitize_text
from vector_db import VectorDB

logger = logging.getLogger(__name__)

_DECISION_PROMPT = (
    "You are the decision module of an autonomous agent. Respond ONLY with a "
    "JSON object with keys: \"action_type\" (one of api_call, database_update, "
    "hardware_interaction, news_api, search_api, multi_step_process, or null), "
    "\"params\" (object), \"confidence\" (number between 0.0 and 1.0), and "
    "\"text\" (short explanation). Do not add any text outside the JSON.\n"
    "Task: {task_json}"
)


class SubconsciousModel:
    """Generates model outputs via pluggable backends (Ollama HTTP default)."""

    def __init__(self, vector_db: Optional[VectorDB] = None,
                 backend: Optional[BaseBackend] = None,
                 backend_config: Optional[Dict[str, Any]] = None,
                 request_timeout: float = 30.0,
                 model_list_cache_seconds: float = 60.0):
        self.vector_db = vector_db
        self.backend = backend if backend is not None else create_backend(backend_config)
        self.request_timeout = max(1.0, float(request_timeout))
        self.model_list_cache_seconds = max(0.0, float(model_list_cache_seconds))
        self.model_success_history: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"successes": 0, "failures": 0}
        )
        self._models_cache: Optional[List[str]] = None
        self._models_cache_at = 0.0
        self._models_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

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
                         backend: Optional[BaseBackend] = None) -> str:
        """
        Query a model with a structured-decision prompt. Returns the raw text
        output ('' on failure), parsed by the decision engine into a proposal.
        ``backend`` overrides the global backend (per-model adapters).
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
        task_payload = (input_data if isinstance(input_data, dict)
                        else {"content": str(input_data)})
        prompt = _DECISION_PROMPT.format(
            task_json=canonical_json(sanitize_text(canonical_json(task_payload), 2000))
        )
        if not validate_model_name(model_name):
            return ""
        try:
            return str(backend.generate(model_name, prompt,
                                        timeout=self.request_timeout))
        except Exception as exc:
            logger.error(f"Model {model_name} call failed: {type(exc).__name__}")
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

