"""
ShugoCore hardened execution layer
==================================

Every decision executes through :meth:`ExecutionLayer.execute`, which:

1. Verifies a policy verdict token bound (by canonical hash) to the exact
   decision - decisions without a valid token are refused (defense in depth:
   the gate lives in the decision engine AND in the executor).
2. Validates every outbound request against the ``CapabilityRegistry``
   (https-only, host allowlist, method allowlist, response size cap).
3. Rate-limits and circuit-breaks per host, with mandatory timeouts.
4. Injects secrets at call time from the ``SecretResolver`` - keys never
   travel inside decision dicts (log redaction is a second safety net).
5. Never simulates success: unimplemented side-effecting actions return
   ``{'status': 'not_implemented'}`` so the reinforcement signal cannot
   reward no-ops.
"""

import json
import logging
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

import requests

from policy import (
    CapabilityRegistry,
    MOBILE_ACTION_TYPES,
    MOBILE_READ_ACTION_TYPES,
    NETWORK_ACTION_TYPES,
    NETWORK_READ_ACTION_TYPES,
    ROBOTICS_ACTION_TYPES,
    ROBOTICS_READ_ACTION_TYPES,
    ROBOTICS_SAFETY_ACTION_TYPES,
    SIDE_EFFECTING_ACTION_TYPES,
)
from security import (
    CircuitBreaker,
    RateLimiter,
    SecretResolver,
    canonical_hash,
    sanitize_text,
)
from telemetry import get_tracer

logger = logging.getLogger(__name__)


class ExecutionLayer:
    """Executes policy-cleared decisions against tools and external APIs."""

    def __init__(self, secrets: Optional[SecretResolver] = None,
                 capabilities: Optional[CapabilityRegistry] = None,
                 rate_limiter: Optional[RateLimiter] = None,
                 audit: Optional[Any] = None,
                 request_timeout: float = 10.0):
        self.secrets = secrets if secrets is not None else SecretResolver()
        self.capabilities = capabilities if capabilities is not None else CapabilityRegistry()
        self.rate_limiter = (rate_limiter if rate_limiter is not None
                             else RateLimiter(calls_per_minute=60))
        self.audit = audit
        self.request_timeout = max(1.0, float(request_timeout))
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        self.tracer = get_tracer("execution_layer")

    def register_handler(self, action_type: str,
                         handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        """
        Operator-installed executor for a side-effecting action type (e.g.
        ``database_update`` backed by a real connection pool) or robotics action
        type. Handlers are invoked only after the full policy gate has cleared
        the decision.
        """
        allowed = (SIDE_EFFECTING_ACTION_TYPES
                   | ROBOTICS_ACTION_TYPES | ROBOTICS_SAFETY_ACTION_TYPES
                   | ROBOTICS_READ_ACTION_TYPES
                   | MOBILE_ACTION_TYPES | MOBILE_READ_ACTION_TYPES
                   | NETWORK_ACTION_TYPES | NETWORK_READ_ACTION_TYPES)
        if action_type not in allowed:
            raise ValueError(f"handlers are only allowed for allowed types "
                             f"{sorted(allowed)}")
        self._handlers[str(action_type)] = handler


    # -- entry point ---------------------------------------------------------

    def execute(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Verify the policy verdict, then dispatch the action."""
        decision = dict(decision or {})
        verdict = decision.get("_policy")
        clean = {k: v for k, v in decision.items() if k != "_policy"}

        if not isinstance(verdict, dict):
            result = {"status": "refused",
                      "reason": "missing policy verdict (fail-closed)"}
        elif verdict.get("verdict") != "allow":
            result = {"status": "refused", "reason": "policy verdict is not 'allow'"}
        elif canonical_hash(clean) != verdict.get("decision_hash"):
            result = {"status": "refused",
                      "reason": "policy verdict does not match decision content"}
        else:
            result = self._dispatch(clean)

        self._audit("execution", {
            "action_type": clean.get("action_type"),
            "status": result.get("status"),
            "reason": sanitize_text(result.get("reason") or result.get("message") or "", 120),
        })
        return result

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        action_type = decision.get("action_type")
        try:
            if action_type == "api_call":
                return self._execute_api_call(decision)
            if action_type == "news_api":
                return self._execute_news_api(decision)
            if action_type == "search_api":
                return self._execute_search_api(decision)
            if action_type == "database_update":
                return self._execute_database_update(decision)
            if action_type == "hardware_interaction":
                return self._execute_hardware_interaction(decision)
            if action_type == "multi_step_process":
                return {"status": "refused",
                        "reason": ("multi_step_process must be expanded and "
                                   "individually gated by the decision engine")}
            return {"status": "error", "message": "Unknown action type"}
        except Exception as exc:  # sanitized: never leak internals to callers
            logger.error(f"Execution failed: {exc}")
            return {"status": "error", "message": type(exc).__name__}

    # -- guarded egress ------------------------------------------------------

    def _guarded_request(self, method: str, url: str,
                         params: Optional[Dict[str, Any]] = None,
                         json_body: Optional[Any] = None) -> Dict[str, Any]:
        """Allowlist -> circuit breaker -> rate limit -> capped HTTPS request."""
        ok, reason = self.capabilities.validate_api_call(url, method)
        if not ok:
            return {"status": "refused", "reason": reason}

        host = urlparse(url).hostname or "unknown"
        breaker = self._breakers.setdefault(host, CircuitBreaker())
        if not breaker.allow():
            return {"status": "refused", "reason": f"circuit open for host '{host}'"}
        if not self.rate_limiter.acquire(host, timeout=5.0):
            return {"status": "refused", "reason": f"rate limit exceeded for host '{host}'"}

        with self.tracer.start_span("http.request",
                                    {"host": host, "method": method}) as span:
            result = self._do_request(method, url, host, breaker, params, json_body)
            span.set_attribute(
                "status", result.get("status", "unknown"))
            span.set_attribute("http_status", result.get("http_status", 0))
        return result

    def _do_request(self, method: str, url: str, host: str, breaker: CircuitBreaker,
                    params: Optional[Dict[str, Any]],
                    json_body: Optional[Any]) -> Dict[str, Any]:
        max_bytes = self.capabilities.max_response_bytes
        try:
            with requests.Session() as session:
                # allow_redirects=False: a redirect could bypass the host allowlist.
                with session.request(method, url, params=params, json=json_body,
                                     timeout=self.request_timeout, stream=True,
                                     allow_redirects=False) as response:
                    chunks = []
                    total = 0
                    for chunk in response.iter_content(chunk_size=8192):
                        total += len(chunk)
                        chunks.append(chunk)
                        if total >= max_bytes:
                            break
                    body = b"".join(chunks)[:max_bytes]
                    encoding = response.encoding or "utf-8"
                    text = body.decode(encoding, errors="replace")
                    http_status = response.status_code
            status_ok = http_status < 400 and http_status != 429
            if status_ok:
                breaker.record_success()
            else:
                breaker.record_failure()
            return {"status": "success" if status_ok else "error",
                    "http_status": http_status,
                    "data": text[:4000],
                    "truncated": total > max_bytes}
        except requests.RequestException as exc:
            breaker.record_failure()
            logger.error(f"Request to '{host}' failed: {exc}")
            return {"status": "error", "message": f"network error: {type(exc).__name__}"}

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception as exc:
            logger.error(f"Audit append failed: {exc}")

    def breakers_open_count(self) -> int:
        """
        Number of hosts whose circuit breakers are currently open. Used by
        the fallback controller's deterministic trigger checks.
        """
        return sum(1 for breaker in self._breakers.values() if breaker.is_open())


    # -- action implementations ----------------------------------------------

    def _execute_api_call(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        params = decision.get("params") or {}
        url = params.get("url") or decision.get("api_endpoint")
        if not url:
            return {"status": "error", "message": "API endpoint not provided"}
        method = str(params.get("method") or "GET").upper()
        body = params.get("payload") if method in ("POST", "PUT", "PATCH") else None
        return self._guarded_request(method, str(url),
                                     params=params.get("query"), json_body=body)

    def _execute_news_api(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        params = decision.get("params") or {}
        query = params.get("query") or decision.get("query")
        if not query:
            return {"status": "error", "message": "Query not provided"}
        api_key = self.secrets.get("news_api_key")
        if not api_key:
            return {"status": "error",
                    "message": "news API key not configured (set SHUGOCORE_NEWS_API_KEY)"}
        return self._guarded_request(
            "GET", "https://newsapi.org/v2/everything",
            params={"q": query, "apiKey": api_key,
                    "language": params.get("language", "en")},
        )

    def _execute_search_api(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        params = decision.get("params") or {}
        query = params.get("query") or decision.get("query")
        if not query:
            return {"status": "error", "message": "Query not provided"}
        result = self._guarded_request(
            "GET", "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1"},
        )
        if result.get("status") == "success":
            try:
                topics = json.loads(result.get("data", "{}")).get("RelatedTopics", [])
                result["results"] = [
                    {"title": item["Text"], "url": item.get("FirstURL")}
                    for item in topics
                    if isinstance(item, dict) and item.get("Text")
                ]
            except (ValueError, AttributeError):
                result["results"] = []
        return result

    def _execute_database_update(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        params = decision.get("params") or {}
        statement = params.get("statement") or decision.get("update_query")
        if not statement:
            return {"status": "error", "message": "database statement not provided"}
        ok, reason = self.capabilities.validate_sql(statement)
        if not ok:
            return {"status": "refused", "reason": reason}
        handler = self._handlers.get("database_update")
        if handler is None:
            return {"status": "not_implemented",
                    "reason": ("no executor registered for 'database_update'; "
                               "actions are never simulated")}
        return handler(decision)

    def _execute_hardware_interaction(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        params = decision.get("params") or {}
        command = params.get("command") or decision.get("command")
        ok, reason = self.capabilities.validate_hardware(command)
        if not ok:
            return {"status": "refused", "reason": reason}
        handler = self._handlers.get("hardware_interaction")
        if handler is None:
            return {"status": "not_implemented",
                    "reason": ("no executor registered for 'hardware_interaction'; "
                               "actions are never simulated")}
        return handler(decision)

    # -- public read-only helper (used by DecisionEngine search/news) ---------

    def http_get_json(self, url: str,
                      params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Allowlisted, rate-limited GET for read-only engine services (search,
        news). Subject to the same egress controls as decision execution.
        """
        return self._guarded_request("GET", url, params=params)


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    execution_layer = ExecutionLayer()

    # Fail-closed demo: no decision may execute without a policy verdict
    # token bound to its exact content (issued by the decision engine).
    ungated_decision = {"action_type": "search_api", "params": {"query": "AI news"}}
    print(execution_layer.execute(ungated_decision))

    # Tampering with a decision invalidates its verdict.
    gated = dict(ungated_decision)
    gated["_policy"] = {"verdict": "allow", "decision_hash": "0" * 64}
    print(execution_layer.execute(gated))


