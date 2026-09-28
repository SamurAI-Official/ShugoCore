#!/usr/bin/env python3
"""Who does what: the hive's capability map, resolved from what nodes advertise.

Authority is the election's (who leads). Capacity is the model host's (whose memory holds
layers). This is the third question, and the one nothing answered: which node *offers a
service* the hive can use -- phrasing, reasoning, perception, being a peripheral -- and
where to reach it.

Each node advertises what it can actually do, with a locator when the service is reachable
over the network, and this resolves a request to a node *and a reason*. Two disciplines are
borrowed from the layer split, because they were learned there:

* **Advertised is not usable.** A capability whose locator does not answer is not offered;
  it is reported as unreachable instead, so a stale advertisement cannot stall the hive.
* **The answer says why.** A resolver that returns "nobody" without a reason turns an
  operator into a detective -- a peer that stayed silent and a fleet that never claimed the
  capability look identical otherwise.

The vocabulary is deliberately short: a capability exists here only when something asks for
it. Adding one means adding its consumer.
"""
import logging
import socket
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# name -> what it means. Keep these honest: they are services a peer can be asked for.
VOCABULARY = {
    "reasoning": "can answer with its own model (a local model backend)",
    "persona": "can phrase a line of speech (an OpenAI-compatible endpoint)",
    "perception": "can report what its sensors see",
    "capacity": "can hold layers of someone else's model (an RPC peripheral)",
}
KNOWN = tuple(sorted(VOCABULARY))
MAX_CAPS_PER_NODE = 8
MAX_LOCATOR_CHARS = 160


def normalise(caps: Any) -> Dict[str, str]:
    """``{capability: locator}`` from whatever a node sent (bounded, unknown dropped).

    A locator is "" when the capability needs no address (perception is "ask me", not
    "call me"). Anything outside the vocabulary is dropped rather than carried around: an
    unknown key is a claim nobody can act on.
    """
    out: Dict[str, str] = {}
    if isinstance(caps, dict):
        items = list(caps.items())
    elif isinstance(caps, (list, tuple)):
        items = [(str(name), "") for name in caps]
    else:
        return out
    for name, locator in items[:MAX_CAPS_PER_NODE]:
        key = str(name or "").strip().lower()
        if key not in VOCABULARY or key in out:
            continue
        out[key] = str(locator or "").strip()[:MAX_LOCATOR_CHARS]
    return out


def claim(*, reasoning: bool = False, persona: str = "", perception: bool = False,
          capacity: bool = False) -> Dict[str, str]:
    """Build this node's own advertisement out of what it is actually running."""
    caps: Dict[str, str] = {}
    if reasoning:
        caps["reasoning"] = ""
    if persona:
        caps["persona"] = str(persona)
    if perception:
        caps["perception"] = ""
    if capacity:
        caps["capacity"] = ""
    return caps


def address_of(locator: str) -> str:
    """``host:port`` out of any locator, so liveness needs no knowledge of the scheme."""
    text = str(locator or "").strip()
    if not text:
        return ""
    if "//" in text:
        text = text.split("//", 1)[1]
    return text.split("/", 1)[0].split("?", 1)[0]


def _resolve_addr(address: str, timeout: float, connect=None) -> bool:
    host, _, port = str(address or "").rpartition(":")
    if not host or not port.isdigit():
        return False
    connector = connect or socket.create_connection
    try:
        with connector((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


class CapabilityMap:
    """The fleet's advertised capabilities, resolvable by name with a reason."""

    def __init__(self, peers: Optional[Iterable[Dict[str, Any]]] = None, *,
                 verify: bool = True, timeout: float = 2.0, connect=None):
        self.timeout = float(timeout or 2.0)
        self.verify = bool(verify)
        self._connect = connect
        self.peers: List[Dict[str, Any]] = [dict(p) for p in (peers or [])
                                            if isinstance(p, dict)]

    def offers(self, capability: str) -> List[Dict[str, Any]]:
        """Live peers that advertise ``capability``, best first, with its locator.

        "Best" is the order the rest of the hive already uses: a reachable locator before
        an unreachable one, then the most measured headroom, then node_id -- so two
        identical checks do not answer differently.
        """
        name = str(capability or "").strip().lower()
        found: List[Dict[str, Any]] = []
        for peer in self.peers:
            caps = normalise(peer.get("caps"))
            if name not in caps:
                continue
            locator = caps[name]
            entry = {"node": str(peer.get("node_id") or peer.get("device_id") or ""),
                     "locator": locator,
                     "mem_available_bytes": peer.get("mem_available_bytes") or 0,
                     "role": str(peer.get("role") or "")}
            if locator and self.verify:
                entry["reachable"] = _resolve_addr(address_of(locator), self.timeout,
                                                   self._connect)
            else:
                entry["reachable"] = True
            found.append(entry)
        found.sort(key=lambda entry: (not entry.get("reachable"),
                                      -int(entry.get("mem_available_bytes") or 0),
                                      entry.get("node")))
        return found

    def resolve(self, capability: str) -> Dict[str, Any]:
        """``{node, locator, reason, offered}``, or a reason when nobody can offer it."""
        name = str(capability or "").strip().lower()
        if name not in VOCABULARY:
            return {"reason": f"unknown capability '{name}'", "offered": []}
        offered = self.offers(name)
        if not offered:
            if not self.peers:
                return {"reason": f"no live peers to offer '{name}'", "offered": []}
            return {"reason": f"no live peer advertises '{name}' "
                              f"({len(self.peers)} live)", "offered": []}
        usable = [entry for entry in offered if entry.get("reachable")]
        if not usable:
            return {"reason": f"every peer advertising '{name}' is unreachable "
                              f"({', '.join(e['node'] for e in offered)})",
                    "offered": offered}
        best = usable[0]
        return {"node": best["node"], "locator": best["locator"],
                "reason": f"{best['node']} offers '{name}'"
                          f"{'' if best['locator'] else ' (no locator needed)'}",
                "offered": offered}

    def summary(self) -> str:
        """One line an operator can read: who offers what, right now."""
        return ",".join(f"{name}->{self.resolve(name).get('node') or 'none'}"
                        for name in KNOWN)
