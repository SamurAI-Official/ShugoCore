"""
ShugoCore context budgeting
===========================

Rigid token allocation across the working-context sections so the context
window cannot overflow before memory compaction triggers.

Token counts use a deterministic, dependency-free heuristic (~4 chars per
token, word-boundary aware floor) - accurate enough for budgeting, and the
counter is pluggable for real tokenizers later.
"""

import re
from typing import Any, Dict, Optional

CHARS_PER_TOKEN = 4
_ELLIPSIS = " …[truncated]"

# Share-based defaults (fractions of the total budget).
DEFAULT_ALLOCATIONS = {
    "scratchpad": 0.30,      # Tier 0 working memory
    "memory_context": 0.25,  # Tier 2 retrieval injected into decisions
    "task": 0.15,            # the task payload itself
    "prompt": 0.20,          # prompt scaffolding
    "params": 0.10,          # action parameters
}


def estimate_tokens(text: Any) -> int:
    """Deterministic token estimate (never zero for non-empty text)."""
    if not text:
        return 0
    text = str(text)
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


class ContextBudget:
    """
    Per-section token budgets. Allocations are fractions of ``total_tokens``
    (values < 1) or absolute token counts (values >= 1).
    """

    def __init__(self, total_tokens: int = 8192,
                 allocations: Optional[Dict[str, float]] = None):
        self.total_tokens = max(256, int(total_tokens))
        shares = {**DEFAULT_ALLOCATIONS, **(allocations or {})}
        self._limits: Dict[str, int] = {}
        for section, value in shares.items():
            if value >= 1:
                self._limits[section] = int(value)
            else:
                self._limits[section] = max(1, int(self.total_tokens * value))

    def limit(self, section: str) -> int:
        return self._limits.get(section, 0)

    def set_limit(self, section: str, tokens: int) -> None:
        self._limits[section] = max(1, int(tokens))

    def fits(self, section: str, text: Any) -> bool:
        return estimate_tokens(text) <= self.limit(section)

    def truncate(self, section: str, text: Any) -> str:
        """Trim ``text`` to the section's token budget (marker appended)."""
        text = str(text)
        limit = self.limit(section)
        if limit <= 0:
            return ""
        if estimate_tokens(text) <= limit:
            return text
        max_chars = max(len(_ELLIPSIS) + 1, limit * CHARS_PER_TOKEN - len(_ELLIPSIS))
        return text[:max_chars] + _ELLIPSIS

    def usage(self, texts: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
        """Snapshot of per-section usage vs. limit (for telemetry/tests)."""
        return {
            section: {"used": estimate_tokens(text), "limit": self.limit(section)}
            for section, text in texts.items()
        }
