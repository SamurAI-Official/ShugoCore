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


def local_address() -> str:
    """This host's address as a peer would reach it. Sends nothing.

    The UDP "connect" only asks the routing table which source address would be used for
    an outbound route; TEST-NET-1 is routeless, so no packet is sent and nothing is
    contacted.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return str(probe.getsockname()[0] or "")
    except Exception:
        return ""


def advertised_locator(url: str, *, host: Optional[str] = None) -> str:
    """Rewrite a locator so a *peer* can use it: loopback becomes this host's address.

    A node configured to call ``127.0.0.1:11434`` is describing where its own backend
    listens, and a peer that adopts that verbatim calls *itself*. The address is therefore
    rewritten to the one peers actually reach, while the port and path stay exactly as
    configured -- and when the address cannot be learned, the claim is dropped rather than
    published unusable.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    prefix = ""
    if "//" in text:
        head, text = text.split("//", 1)
        prefix = head + "//"
    host_part, sep, tail = text.partition(":")
    if host_part.strip().lower() in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        reachable = str(host or "").strip() or local_address()
        if not reachable:
            return ""
        host_part = reachable
    return f"{prefix}{host_part}{sep}{tail}"


def is_local_locator(url: str) -> bool:
    """True when a URL points at this node itself.

    The distinction that matters for advertisement: a node *serving* a model points at its
    own loopback, while a node *calling* someone else's points at theirs -- and only the
    first can offer that service to the hive.
    """
    text = str(url or "").strip()
    if "//" in text:
        text = text.split("//", 1)[1]
    host = text.partition(":")[0].strip().lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "")


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
                 verify: bool = True, timeout: float = 2.0, connect=None,
                 hosts: Optional[Dict[str, str]] = None):
        self.timeout = float(timeout or 2.0)
        self.verify = bool(verify)
        self._connect = connect
        # node_id -> the address its peers dial (from the mesh peer map). Used to make
        # sense of a locator that says "here", which means nothing to anyone else.
        self.hosts: Dict[str, str] = {str(k): str(v) for k, v in (hosts or {}).items()}
        self.peers: List[Dict[str, Any]] = [dict(p) for p in (peers or [])
                                            if isinstance(p, dict)]

    def _reachable_locator(self, locator: str, node: str) -> Dict[str, Any]:
        """A locator a peer can dial, saying so when its host had to be substituted."""
        text = str(locator or "").strip()
        # Work on the address, not the URL: partitioning on the first colon of
        # "http://127.0.0.1:11434" gives the scheme's colon, not the port's.
        address = address_of(text)
        host, _, tail = address.partition(":")
        if host.strip().lower() not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return {"locator": text, "substituted": False}
        known_host, _, known_port = address_of(self.hosts.get(node, "")).rpartition(":")
        if not known_host:
            return {"locator": text, "substituted": False}
        port = tail.split("/", 1)[0] or known_port
        path = "/" + tail.split("/", 1)[1] if "/" in tail else ""
        prefix = text.split("//", 1)[0] + "//" if "//" in text else ""
        return {"locator": f"{prefix}{known_host}:{port}{path}", "substituted": True}

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
            entry["substituted"] = False
            if entry["locator"]:
                usable = self._reachable_locator(entry["locator"], entry["node"])
                entry["locator"] = usable["locator"]
                entry["substituted"] = usable["substituted"]
                entry["reachable"] = (_resolve_addr(address_of(entry["locator"]),
                                                    self.timeout, self._connect)
                                      if self.verify else True)
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
        note = " (at the peer's own address)" if best.get("substituted") else ""
        return {"node": best["node"], "locator": best["locator"],
                "reason": f"{best['node']} offers '{name}'{note}"
                          f"{'' if best['locator'] else ' (no locator needed)'}",
                "offered": offered}

    def summary(self) -> str:
        """One line an operator can read: who offers what, right now."""
        return ",".join(f"{name}->{self.resolve(name).get('node') or 'none'}"
                        for name in KNOWN)
