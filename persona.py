#!/usr/bin/env python3
"""Phrasing as a service: the persona model decides *how* the hive says something.

The primary decides *what* to say (the reasoning model, annotated by the personality
governor) and the device nearest the operator says it (`response_routing`). This is the
stage in between: the wording, produced by a model that lives on another node -- the Mac
in this fleet -- which is cheap to lose and never in charge.

Three properties hold by construction:

* **It shapes, it never approves.** The shaper runs *before* the safety gate, on the
  decision the governor already annotated, so the gate checks the words that will
  actually be spoken. A persona model cannot approve anything.
* **Fail-open on style, never on safety.** No endpoint, a timeout, a malformed reply --
  each returns the primary's own draft with the reason recorded. The hive does not lose
  its voice because a peripheral is down.
* **Both drafts are kept.** The caller journals the draft and the spoken line, so "what
  the persona changed" is answerable afterwards rather than guessed at.

The persona model is driven entirely by instructions the *primary* supplies (its
personality text and the governor's verdict). That is deliberate: the trait vector stays
on the node that owns the agent, and the node that phrases is replaceable.
"""
import json
import logging
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 8.0


def _chat_endpoint(url: str) -> str:
    """``host:port`` or a base URL -> the chat-completions URL to post to."""
    base = str(url or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    if not base.endswith("/v1/chat/completions"):
        base = base + "/v1/chat/completions"
    return base


def _post_chat(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """POST an OpenAI-compatible chat request and return the decoded body."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace") or "{}")


class PersonaShaper:
    """Rewrite a line of speech with a model on another node. Never required."""

    def __init__(self, url: str = "", model: str = "", *,
                 timeout: float = DEFAULT_TIMEOUT_S,
                 poster=None, instructions: str = ""):
        self.url = _chat_endpoint(url)
        self.model = str(model or "").strip() or "persona"
        try:
            self.timeout = max(0.5, float(timeout or DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT_S
        self._post = poster or _post_chat
        # The character text the primary owns and hands over per call.
        self.instructions = str(instructions or "")
        # Set by the caller when the endpoint is to be resolved from the fleet: a shaper
        # that is merely unconfigured and one that is waiting for a service to appear look
        # identical otherwise, and only the second should be waited for.
        self.auto = False
        self.shaped = 0
        self.fallbacks = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def label(self) -> str:
        """Short form for a status line: ``persona=<model>@<host>``, or ``off``."""
        if not self.enabled:
            return "off"
        host = self.url.split("//", 1)[-1].split("/", 1)[0]
        return f"{self.model}@{host}"

    def _prompt(self, verdict: Optional[Dict[str, Any]]) -> str:
        """What the persona model is told: the character, and today's verdict."""
        lines = [self.instructions.strip()] if self.instructions.strip() else []
        lines.append(
            "Rewrite the line below so it sounds like this character. Keep the meaning, "
            "the facts and any names exactly; keep it as spoken language. Answer with "
            "the rewritten line only -- no quotes, no commentary.")
        verdict = verdict if isinstance(verdict, dict) else {}
        detail = []
        if verdict.get("tone"):
            detail.append(f"tone: {verdict['tone']}")
        budget = verdict.get("max_sentences") or verdict.get("verbosity")
        if budget:
            detail.append(f"length: {budget}")
        if verdict.get("appropriateness") is not None:
            detail.append(f"appropriateness: {verdict['appropriateness']}")
        if detail:
            lines.append("Governor: " + "; ".join(str(d) for d in detail) + ".")
        return "\n".join(lines)

    def shape(self, text: str, *, verdict: Optional[Dict[str, Any]] = None,
              instructions: str = "") -> Dict[str, Any]:
        """Return ``{text, source, reason}``; source is persona, draft or unavailable.

        Every failure path returns the caller's own text: a hive that cannot reach the
        node that phrases still speaks.
        """
        draft = str(text or "")
        if not self.enabled or not draft.strip():
            return {"text": draft, "source": "draft", "draft": draft,
                    "reason": ("no persona endpoint" if not self.enabled
                               else "nothing to say")}
        character = str(instructions or self.instructions or "").strip()
        payload = {
            "model": self.model,
            "temperature": 0.7,
            "max_tokens": 220,
            "messages": [
                {"role": "system", "content": self._prompt(verdict)},
                {"role": "user", "content": draft},
            ],
        }
        try:
            body = self._post(self.url, payload, self.timeout)
            shaped = _first_message(body.get("choices") if isinstance(body, dict)
                                    else None)
            if not shaped:
                raise ValueError("empty completion")
            self.shaped += 1
            return {"text": shaped, "source": "persona", "reason": "",
                    "draft": draft, "instructions": character}
        except Exception as exc:
            self.fallbacks += 1
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("persona unavailable (%s); speaking the draft", reason)
            return {"text": draft, "source": "unavailable", "reason": reason,
                    "draft": draft}


def _first_message(choices) -> str:
    """The text out of an OpenAI-compatible reply, or "" when it has none."""
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    text = message.get("content") or first.get("text") or ""
    return str(text).strip().strip('"').strip()
