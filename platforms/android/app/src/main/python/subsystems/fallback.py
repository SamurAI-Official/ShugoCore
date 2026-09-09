"""
Error handling & graceful fallback (Phase 3.3).

Adds retry-with-backoff and strategy chains so Shugo degrades gracefully
instead of failing hard when a tool or backend is flaky:

    RetryPolicy  — exponential backoff, configurable attempts
    retry_call   — apply a RetryPolicy to any callable
    FallbackChain — try strategies in order until one succeeds

Pure stdlib and dependency-free. Used by the command executor and the
agent's conversational handling.
"""
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ResultT = Tuple[bool, Any]


class RetryPolicy:
    """How many times to retry a callable and how to back off."""

    def __init__(self, max_attempts: int = 2, base_backoff_secs: float = 0.2,
                 backoff_factor: float = 2.0,
                 allowed_exceptions: Optional[Tuple[type, ...]] = None,
                 on_retry: Optional[Callable[[Exception, int], None]] = None):
        self.max_attempts = max(1, max_attempts)
        self.base_backoff_secs = max(0.0, base_backoff_secs)
        self.backoff_factor = max(1.0, backoff_factor)
        self.allowed_exceptions = allowed_exceptions or (Exception,)
        self.on_retry = on_retry

    def sleep_before_retry(self, attempt: int) -> None:
        """attempt is 0-based (0 = first retry after the first failure)."""
        delay = self.base_backoff_secs * (self.backoff_factor ** attempt)
        if delay > 0:
            time.sleep(min(delay, 5.0))


def retry_call(func: Callable[..., Any], policy: RetryPolicy,
               *args: Any, **kwargs: Any) -> Tuple[bool, Any]:
    """Call func with retry. Returns (ok, result_or_last_exception)."""
    last_error: Optional[Exception] = None
    for attempt in range(policy.max_attempts):
        try:
            return True, func(*args, **kwargs)
        except policy.allowed_exceptions as exc:  # type: ignore[arg-type]
            last_error = exc
            is_last = attempt == policy.max_attempts - 1
            if not is_last:
                if policy.on_retry is not None:
                    try:
                        policy.on_retry(exc, attempt)
                    except Exception:
                        pass
                logger.warning("retry %s/%s after %r",
                               attempt + 1, policy.max_attempts, exc)
                policy.sleep_before_retry(attempt)
    return False, last_error


class FallbackChain:
    """A list of (name, callable) strategies tried in order.

    Each strategy returns (ok, result). The first ok=True wins. If all fail,
    the last strategy's result is returned (so the final fallback can be a
    guaranteed-ok apology that never raises).
    """

    def __init__(self):
        self._strategies: List[Tuple[str, Callable[[], ResultT]]] = []

    def add(self, name: str, strategy: Callable[[], ResultT]) -> None:
        self._strategies.append((name, strategy))

    def names(self) -> List[str]:
        return [name for name, _ in self._strategies]

    def run(self) -> Tuple[str, bool, Any]:
        """Run strategies in order. Returns (name, ok, result)."""
        last_name = "none"
        last_ok = False
        last_result: Any = None
        for name, strategy in self._strategies:
            last_name = name
            try:
                ok, result = strategy()
            except Exception as exc:
                logger.error("Fallback strategy '%s' raised: %s", name, exc)
                ok, result = False, exc
            last_ok, last_result = ok, result
            if ok:
                logger.debug("Fallback resolved via %r", name)
                return name, True, result
        return last_name, last_ok, last_result

    def __len__(self) -> int:
        return len(self._strategies)