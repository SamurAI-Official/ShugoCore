#!/usr/bin/env python3
"""
ShugoCore netwatch -- link-cycle timeline recorder
==================================================

Records a 1 Hz timeline of the local link so that an external "network cycle"
-- a gateway re-publishing its RA/RDNSS/DHCPv6 data, a NAT64/DNS64
re-provision, a VPN or relay drop, an interface flap, or a software-update
retry storm -- can be *correlated* with what a node actually observed instead
of guessed at.

Why this exists
---------------
The Shogunet bridge already names the two link failure modes that matter to a
fleet (``shugonet_bridge.NETWORK_FALLBACK_SEVERITIES``)::

    network_transport_exhausted  -> pause
    network_peer_lost            -> pause

but nothing emits them while the node runtime is idle, so an outage that
happens between runs leaves no ShugoCore-side trace at all. netwatch fills
that gap: it samples the link with no node running and writes one JSON line
per tick.

Records reuse the node audit chain's field names (``timestamp`` / ``type`` /
``payload`` / ``seq``) so the timeline diffs directly against
``node_audit.jsonl`` and against system logs (for example
``/var/log/install.log`` during a macOS upgrade) at second resolution. They are
*plain* JSONL: unlike ``audit.py`` records they are not hash-chained and must
not be treated as tamper-evident.

Probe design
------------
* **stdlib only, no root.** Reachability is a TCP connect and DNS is
  ``getaddrinfo``, so no ICMP and no privileges are needed. The local address
  is learned with a UDP ``connect()``, which sends no packets.
* **Injected clock and probe.** ``NetProbe`` is the real implementation;
  ``NetWatcher`` only calls ``now()``, ``sleep()``, ``wall_now()``,
  ``local_address()``, ``default_gateway()``, ``resolve_ms()`` and
  ``connect()``, which is how the tests drive it deterministically without
  touching the network.

State classification
--------------------
Precedence: ``no_route`` > ``transport_exhausted`` > ``dns_failure`` >
``degraded`` > ``ok``.

- ``no_route``: no local address / no default route (the literal cycle).
- ``transport_exhausted``: nothing on any configured endpoint answered ->
  emits the ``network_transport_exhausted`` record with the fallback severity
  ``pause``.
- ``dns_failure``: resolution failed while the path still answered.
- ``degraded``: every probe answered but one exceeded ``--slow-ms``.
- ``ok``: everything answered promptly.

Each endpoint is judged ``open`` (handshake completed), ``refused`` (the peer
sent a reset) or ``unreachable`` (timeout / no route / would not resolve).
**A refusal counts as the path working** -- the packet arrived and something
answered -- so a node whose ``--target`` port is simply closed is not reported
as an outage. That distinction matters on carrier/CGNAT gateways, which
routinely reset TCP to addresses they intercept, so the default ``--internet``
list spans two operators and a single intercepted address cannot fake a total
outage. The ``--target`` endpoint only counts as path evidence when it is
off-box: a loopback refusal is answered by the local stack no matter what the
link is doing, which each record reports as ``target_authoritative``.

``dns_failure`` is the signature of a gateway re-publishing its RA/RDNSS/
DHCPv6 data (and of NAT64 re-provisioning): established flows keep working
while every *new* connection fails, which is why a download or update job
notices first and everything else looks fine.

Usage::

    # sample the local engine and Apple's update host for an hour
    python3 scripts/netwatch.py --jsonl /tmp/netwatch.jsonl \
        --dns-name swscan.apple.com --duration 3600

    # what the node saw, second by second
    python3 -c "import json; print([r['payload']['state'] for r in \\
        map(json.loads, open('/tmp/netwatch.jsonl')) \\
        if r['type'] == 'netwatch_tick'])"

    # what the OS reported at the same second
    grep -n 'NSURLErrorDomain Code=-1009' /var/log/install.log

Exit status is ``0`` when no cycle was seen, ``1`` when at least one was and
``2`` for a usage error, so the sampler can gate a cron job or a soak run.
"""


import argparse
import json
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

try:  # keep the emitted severity in sync with the fallback controller's table
    from shugonet_bridge import NETWORK_FALLBACK_SEVERITIES as _NETWORK_SEVERITIES
except ImportError:  # run as a loose script: fall back to the documented value
    _NETWORK_SEVERITIES = {}

# Mirrors the ``network_transport_exhausted`` trigger in shugonet_bridge.py.
NETWORK_TRANSPORT_SEVERITY = str(
    _NETWORK_SEVERITIES.get("network_transport_exhausted", "pause")
)

DEFAULT_TARGET = "127.0.0.1:11435"   # shugocore-server's documented default port
# Numeric literals (no DNS needed) from two different operators, so a gateway
# that intercepts or blackholes one of them cannot fake a total outage.
DEFAULT_INTERNET = "9.9.9.9:443,1.1.1.1:443"
DEFAULT_DNS_NAME = "apple.com"       # use swscan.apple.com to match update logs
DEFAULT_INTERVAL = 1.0
DEFAULT_TIMEOUT = 1.5
DEFAULT_SLOW_MS = 1000.0
RECORD_HISTORY = 256                 # ring buffer kept for introspection

STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_DNS_FAILURE = "dns_failure"
STATE_TRANSPORT_EXHAUSTED = "transport_exhausted"
STATE_NO_ROUTE = "no_route"

# States that count as an outage, and therefore as a cycle.
OUTAGE_STATES = (STATE_DNS_FAILURE, STATE_TRANSPORT_EXHAUSTED, STATE_NO_ROUTE)

# TCP connect outcomes. A refused connect is *not* a failure: the peer answered
# with a reset, which proves the path works and only says nothing is listening.
CONNECT_OPEN = "open"
CONNECT_REFUSED = "refused"
CONNECT_UNREACHABLE = "unreachable"
REACHABLE_RESULTS = (CONNECT_OPEN, CONNECT_REFUSED)


def parse_endpoint(value: Any, default_port: int = 443) -> Tuple[str, int]:
    """``host:port`` / ``host`` / ``[v6]:port`` / bare v6 literal -> (host, port).

    Raises ``ValueError`` for an empty endpoint or a non-numeric port, so the
    CLI can turn it into exit status 2 instead of a traceback.
    """
    text = str(value).strip()
    if not text:
        raise ValueError("empty endpoint")
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        if not host:
            raise ValueError("empty host in endpoint: %r" % (value,))
        return host, int(rest.lstrip(":") or default_port)
    if text.count(":") == 1:
        host, _, port_text = text.partition(":")
        if not host:
            raise ValueError("empty host in endpoint: %r" % (value,))
        return host, int(port_text or default_port)
    if text.count(":") > 1:  # bare IPv6 literal, no brackets
        return text, int(default_port)
    return text, int(default_port)


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` / ``127.0.0.0/8`` / ``::1``.

    A loopback endpoint is never evidence about the network path: the local
    stack answers whether or not the link is up.
    """
    text = str(host or "").strip().lower()
    if not text:
        return False
    if text in ("localhost", "::1", "0:0:0:0:0:0:0:1"):
        return True
    return text.startswith("127.")


def parse_endpoints(value: Any, default_port: int = 443) -> List[Tuple[str, int]]:
    """Comma-separated ``host:port`` list -> ``[(host, port), ...]``.

    An empty/blank value yields an empty list, which disables those probes.
    """
    text = str(value or "").strip()
    if not text:
        return []
    return [parse_endpoint(part.strip(), default_port) for part in text.split(",") if part.strip()]


def probe_result(value: Any) -> Dict[str, Any]:
    """Normalise a connect outcome to ``{"result": str, "ms": Optional[float]}``.

    Accepts the dict a :class:`NetProbe` returns, or a bare number/None so a
    hand-written probe stays concise (a number means "answered in that many
    milliseconds", ``None`` means unreachable).
    """
    if isinstance(value, dict):
        result = str(value.get("result") or CONNECT_UNREACHABLE)
        ms = value.get("ms")
    elif value is None:
        return {"result": CONNECT_UNREACHABLE, "ms": None}
    else:
        result, ms = CONNECT_OPEN, value
    try:
        ms = float(ms) if ms is not None else None
    except (TypeError, ValueError):
        ms = None
    return {"result": result, "ms": ms}


def is_reachable(value: Any) -> bool:
    """True when the path answered at all (open *or* refused)."""
    return probe_result(value)["result"] in REACHABLE_RESULTS


def aggregate_result(probes: List[Dict[str, Any]]) -> str:
    """One result string for a group of endpoints: best outcome wins."""
    results = [probe_result(item)["result"] for item in probes]
    if not results:
        return "none"
    for candidate in (CONNECT_OPEN, CONNECT_REFUSED):
        if candidate in results:
            return candidate
    return CONNECT_UNREACHABLE


def fastest_ms(probes: List[Dict[str, Any]]) -> Optional[float]:
    """Fastest measured answer across a group of endpoints, or None."""
    values = [probe_result(item)["ms"] for item in probes]
    measured = [value for value in values if value is not None]
    return min(measured) if measured else None


def parse_route_output(text: Any) -> Optional[str]:
    """Pull the gateway address out of ``route -n get default`` output."""
    for line in str(text).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "gateway":
            return value.strip() or None
    return None


def parse_proc_net_route(text: Any) -> Optional[str]:
    """Pull the default gateway out of Linux ``/proc/net/route``.

    Addresses are little-endian hex per field, so octets come back reversed.
    """
    for line in str(text).splitlines()[1:]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        raw = fields[2]
        if len(raw) != 8:
            continue
        octets = [str(int(raw[index:index + 2], 16)) for index in (6, 4, 2, 0)]
        return ".".join(octets)
    return None


def classify(route_present: bool,
             local_addr: Optional[str],
             dns_ms: Optional[float],
             internet: Any,
             target: Any = None,
             gateway: Any = None,
             slow_ms: float = DEFAULT_SLOW_MS) -> str:
    """Map probe outcomes to one link state.

    ``internet`` is a list of connect outcomes (one per endpoint); ``target``
    and ``gateway`` are single outcomes, in any shape :func:`probe_result`
    accepts. The path counts as alive when *any* of them answered -- including
    a refusal, because a reset proves the packet got there.

    ``gateway`` is informational only: a gateway that filters HTTP or ICMP is
    normal, so a failed gateway probe never defines the state by itself.
    """
    if not route_present or local_addr is None:
        return STATE_NO_ROUTE
    internet_probes = internet if isinstance(internet, list) else [internet]
    answered = any(is_reachable(item) for item in internet_probes)
    if not answered and target is not None:
        answered = is_reachable(target)
    if not answered:
        return STATE_TRANSPORT_EXHAUSTED
    if dns_ms is None:
        return STATE_DNS_FAILURE
    latencies = [probe_result(item)["ms"] for item in internet_probes]
    for extra in (target, gateway):
        if extra is not None:
            latencies.append(probe_result(extra)["ms"])
    latencies.append(dns_ms)
    measured = [value for value in latencies if value is not None]
    if measured and max(measured) >= slow_ms:
        return STATE_DEGRADED
    return STATE_OK


class NetProbe:
    """Real stdlib probes: no root, no ICMP, no third-party packages."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = float(timeout)

    def now(self) -> float:
        """Monotonic clock used for all durations."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wall_now(self) -> datetime:
        """Wall clock used for record timestamps (audit chain uses UTC)."""
        return datetime.now(timezone.utc)

    def local_address(self) -> Optional[str]:
        """Local address used for the default route.

        A UDP ``connect()`` only sets the socket's peer, so this sends no
        packets; it fails when there is no route to the destination.
        """
        endpoints = parse_endpoints(DEFAULT_INTERNET)
        if not endpoints:
            return None
        host, port = endpoints[0]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect((host, port))
            addr = sock.getsockname()
            return addr[0] if addr else None
        except OSError:
            return None
        finally:
            try:
                sock.close()
            except OSError:  # pragma: no cover - close() on a dead socket
                pass

    def default_gateway(self) -> Optional[str]:
        """Default gateway, or None when it cannot be determined."""
        try:
            completed = subprocess.run(
                ["route", "-n", "get", "default"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            gateway = parse_route_output(completed.stdout.decode("utf-8", "replace"))
            if gateway:
                return gateway
        try:
            with open("/proc/net/route", "r", encoding="utf-8") as handle:
                return parse_proc_net_route(handle.read())
        except OSError:
            return None

    def resolve_ms(self, name: str) -> Optional[float]:
        """Resolve ``name`` and return the elapsed milliseconds, or None."""
        start = self.now()
        try:
            socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return None
        return (self.now() - start) * 1000.0

    def connect(self, host: str, port: int) -> Dict[str, Any]:
        """TCP-connect to ``host:port`` -> ``{"result": str, "ms": float|None}``.

        ``result`` is :data:`CONNECT_OPEN` (handshake completed),
        :data:`CONNECT_REFUSED` (the peer answered with a reset: the path works,
        nothing is listening there) or :data:`CONNECT_UNREACHABLE` (timeout, no
        route, or the name would not resolve). ``ms`` is set whenever something
        answered, including a refusal, so an intercepted network still yields a
        latency sample.

        Name resolution is deliberately excluded from ``ms``: DNS is probed
        separately so a resolver failure is not reported as a dead path.
        """
        try:
            infos = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return {"result": CONNECT_UNREACHABLE, "ms": None}
        if not infos:
            return {"result": CONNECT_UNREACHABLE, "ms": None}
        family, socktype, proto, _, sockaddr = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(self.timeout)
        start = self.now()
        try:
            sock.connect(sockaddr)
            return {"result": CONNECT_OPEN, "ms": (self.now() - start) * 1000.0}
        except ConnectionRefusedError:
            return {"result": CONNECT_REFUSED, "ms": (self.now() - start) * 1000.0}
        except OSError:
            return {"result": CONNECT_UNREACHABLE, "ms": None}
        finally:
            try:
                sock.close()
            except OSError:  # pragma: no cover - close() on a dead socket
                pass


def sample(probe: Any,
           dns_name: Optional[str] = DEFAULT_DNS_NAME,
           target: str = DEFAULT_TARGET,
           internet: str = DEFAULT_INTERNET,
           gateway_port: Optional[int] = None,
           slow_ms: float = DEFAULT_SLOW_MS) -> Dict[str, Any]:
    """Take one measurement and return it as a record payload dict.

    ``internet`` accepts a comma-separated list of ``host:port`` endpoints and
    the path counts as alive when *any* of them answers. Pass ``dns_name=None``
    to skip the resolver probe.
    """
    target_host, target_port = parse_endpoint(target)

    def probe_one(host: str, port: int) -> Dict[str, Any]:
        outcome = dict(probe_result(probe.connect(host, port)))
        outcome["endpoint"] = "%s:%d" % (host, port)
        return outcome

    gateway = probe.default_gateway()
    local_addr = probe.local_address()
    dns_ms = probe.resolve_ms(dns_name) if dns_name else None
    internet_probes = [probe_one(host, port) for host, port in parse_endpoints(internet)]
    target_probe = probe_one(target_host, target_port)
    gateway_probe = probe_one(gateway, int(gateway_port)) if (gateway and gateway_port) else None
    # The node port is only evidence about the *path* when it is off-box; a
    # loopback refusal is answered by the local stack regardless of the link.
    target_authoritative = not is_loopback_host(target_host)
    payload: Dict[str, Any] = {
        "local_addr": local_addr,
        "gateway": gateway,
        # A gateway can be undeterminable without the link being down, so a
        # working local address counts as a route on its own.
        "route_present": bool(gateway) or bool(local_addr),
        "dns_name": dns_name,
        "dns_ms": dns_ms,
        "target": target_probe,
        "target_authoritative": target_authoritative,
        "internet": internet_probes,
        "gateway_probe": gateway_probe,
        # Flat copies of the group summaries: easier to grep and to diff
        # against a log than nested structures.
        "target_result": target_probe["result"],
        "target_ms": target_probe["ms"],
        "internet_result": aggregate_result(internet_probes),
        "internet_ms": fastest_ms(internet_probes),
    }
    payload["state"] = classify(
        route_present=payload["route_present"],
        local_addr=local_addr,
        dns_ms=dns_ms,
        internet=internet_probes,
        target=target_probe if target_authoritative else None,
        gateway=gateway_probe,
        slow_ms=slow_ms,
    )
    # Local-time copy: system logs (install.log, pmset, log show) are local.
    payload["local_time"] = probe.wall_now().astimezone().isoformat(timespec="seconds")
    return payload



class JsonlWriter:
    """Append-only JSONL sink: one record per line, audit-compatible keys."""

    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        self._handle = None

    def open(self) -> "JsonlWriter":
        self._handle = self.path.open("a", encoding="utf-8")
        return self

    def write(self, record: Dict[str, Any]) -> None:
        if self._handle is None:
            self.open()
        self._handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "JsonlWriter":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


class NetWatcher:
    """Turns probe samples into audit-shaped records and cycle statistics.

    The watcher only talks to whatever ``probe`` it is handed, so a fake probe
    (injected clock + scripted results) fully drives it in tests.
    """

    def __init__(self,
                 probe: Any = None,
                 dns_name: Optional[str] = DEFAULT_DNS_NAME,
                 target: str = DEFAULT_TARGET,
                 internet: str = DEFAULT_INTERNET,
                 gateway_port: Optional[int] = None,
                 slow_ms: float = DEFAULT_SLOW_MS) -> None:
        self.probe = probe if probe is not None else NetProbe()
        self.dns_name = dns_name
        self.target = target
        self.internet = internet
        self.gateway_port = gateway_port
        self.slow_ms = slow_ms
        self.seq = 0
        self.ticks = 0
        self.cycles = 0
        self.state: Optional[str] = None
        self.started_at: Optional[str] = None
        self.ended_at: Optional[str] = None
        self.counts: Dict[str, int] = {
            STATE_OK: 0,
            STATE_DEGRADED: 0,
            STATE_DNS_FAILURE: 0,
            STATE_TRANSPORT_EXHAUSTED: 0,
            STATE_NO_ROUTE: 0,
        }
        self.outages: List[Dict[str, Any]] = []
        self.records: Deque[Dict[str, Any]] = deque(maxlen=RECORD_HISTORY)
        self._outage: Optional[Dict[str, Any]] = None

    # -- records -----------------------------------------------------------
    def record(self, record_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Build one record, store it in the ring buffer and return it."""
        self.seq += 1
        record = {
            "timestamp": self.probe.wall_now().isoformat(),
            "type": record_type,
            "seq": self.seq,
            "payload": payload,
        }
        self.records.append(record)
        return record

    # -- sampling ----------------------------------------------------------
    def tick(self) -> List[Dict[str, Any]]:
        """Sample once; return the records it produced, oldest first.

        An outage that starts emits ``network_cycle`` (and
        ``network_transport_exhausted`` when every direct-IP connect failed);
        the tick that ends it emits ``network_recovered``. ``degraded`` is
        reported in the tick stream but never counts as a cycle.
        """
        payload = sample(
            self.probe,
            dns_name=self.dns_name,
            target=self.target,
            internet=self.internet,
            gateway_port=self.gateway_port,
            slow_ms=self.slow_ms,
        )
        state = str(payload["state"])
        self.ticks += 1
        self.counts[state] = self.counts.get(state, 0) + 1
        if self.started_at is None:
            self.started_at = payload["local_time"]
        self.ended_at = payload["local_time"]

        emitted = [self.record("netwatch_tick", payload)]
        previous = self.state
        if state in OUTAGE_STATES and previous not in OUTAGE_STATES:
            self.cycles += 1
            self._outage = {
                "cycle": self.cycles,
                "state": state,
                "start": payload["local_time"],
                "start_clock": self.probe.now(),
            }
            emitted.append(self.record("network_cycle", {
                "cycle": self.cycles,
                "state": state,
                "previous_state": previous or STATE_OK,
                "gateway": payload["gateway"],
                "local_addr": payload["local_addr"],
                "dns_ms": payload["dns_ms"],
                "target_result": payload["target_result"],
                "target_ms": payload["target_ms"],
                "internet_result": payload["internet_result"],
                "internet_ms": payload["internet_ms"],
            }))
            if state == STATE_TRANSPORT_EXHAUSTED:
                emitted.append(self.record("network_transport_exhausted", {
                    "severity": NETWORK_TRANSPORT_SEVERITY,
                    "target": dict(payload["target"]),
                    "internet": [dict(item) for item in payload["internet"]],
                    "gateway": payload["gateway"],
                    "detail": "every configured endpoint was unreachable",
                }))
        elif state not in OUTAGE_STATES and previous in OUTAGE_STATES:
            outage = self._outage or {"cycle": self.cycles, "state": previous}
            start_clock = float(outage.pop("start_clock", self.probe.now()))
            outage["end"] = payload["local_time"]
            outage["outage_seconds"] = round(self.probe.now() - start_clock, 3)
            self.outages.append(outage)
            self._outage = None
            emitted.append(self.record("network_recovered", {
                "cycle": outage.get("cycle"),
                "state": state,
                "previous_state": previous,
                "outage_seconds": outage["outage_seconds"],
            }))
        self.state = state
        return emitted


    # -- reporting ---------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        """Snapshot of what the run saw, safe to call after an interrupt."""
        closed = list(self.outages)
        if self._outage is not None:  # still down when sampling stopped
            open_outage = dict(self._outage)
            open_outage["end"] = None
            open_outage["outage_seconds"] = round(
                self.probe.now() - float(open_outage.get("start_clock", self.probe.now())), 3
            )
            open_outage.pop("start_clock", None)
            closed.append(open_outage)
        durations = [float(o.get("outage_seconds") or 0.0) for o in closed]
        return {
            "ticks": self.ticks,
            "cycles": self.cycles,
            "states": dict(self.counts),
            "outages": closed,
            "outage_seconds_total": round(sum(durations), 3),
            "longest_outage_seconds": round(max(durations), 3) if durations else 0.0,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "target": self.target,
            "internet": self.internet,
            "dns_name": self.dns_name,
        }

    def summary_lines(self) -> List[str]:
        """Operator-facing summary, one line per fact."""
        data = self.summary()
        states = data.get("states") or {}
        lines = [
            "netwatch: %d ticks, %d cycles, %s down time"
            % (data["ticks"], data["cycles"], _format_seconds(data["outage_seconds_total"])),
            "  link health: " + " ".join(
                "%s=%d" % (state, int(states.get(state, 0)))
                for state in (STATE_OK, STATE_DEGRADED, STATE_DNS_FAILURE,
                              STATE_TRANSPORT_EXHAUSTED, STATE_NO_ROUTE)
            ),
            "  window: %s -> %s" % (data["started_at"], data["ended_at"]),
        ]
        for index, outage in enumerate(data.get("outages") or [], start=1):
            lines.append("  outage #%d: %s -> %s (%s, %s)" % (
                index,
                outage.get("start"),
                outage.get("end") or "still down",
                _format_seconds(outage.get("outage_seconds") or 0.0),
                outage.get("state"),
            ))
        return lines

    # -- loop --------------------------------------------------------------
    def run(self,
            interval: float = DEFAULT_INTERVAL,
            duration: Optional[float] = None,
            ticks: Optional[int] = None,
            on_record: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """Sample until ``ticks`` or ``duration`` is reached, then return a summary.

        With neither bound the loop runs until interrupted (``KeyboardInterrupt``
        is left to the caller). ``on_record`` is invoked for every emitted record.
        """
        started = self.probe.now()
        while True:
            for record in self.tick():
                if on_record is not None:
                    on_record(record)
            if ticks is not None and self.ticks >= int(ticks):
                break
            if duration is not None and (self.probe.now() - started) >= float(duration):
                break
            self.probe.sleep(float(interval))
        return self.summary()


def _format_seconds(value: Any) -> str:
    """Seconds as a compact duration string."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "?"
    if seconds < 60:
        return "%.1fs" % seconds
    return "%dm%02ds" % (int(seconds // 60), int(seconds % 60))


def _format_ms(value: Any) -> str:
    """Milliseconds for the operator line, ``-`` when the probe failed."""
    if value is None:
        return "-"
    try:
        return "%.1fms" % float(value)
    except (TypeError, ValueError):
        return "-"


def _describe(result: Any, ms: Any) -> str:
    """``result`` plus its latency for the operator line ("open/87.7ms")."""
    text = str(result or "unknown")
    formatted = _format_ms(ms)
    return text if formatted == "-" else "%s/%s" % (text, formatted)


def format_record(record: Dict[str, Any]) -> str:
    """One operator-facing line per record."""
    payload = record.get("payload") or {}
    stamp = str(record.get("timestamp") or "")[11:19]
    kind = str(record.get("type") or "")
    state = str(payload.get("state") or "")
    if kind == "netwatch_tick":
        return "%s  %-19s dns=%s target=%s internet=%s" % (
            stamp, state,
            _format_ms(payload.get("dns_ms")),
            _describe(payload.get("target_result"), payload.get("target_ms")),
            _describe(payload.get("internet_result"), payload.get("internet_ms")),
        )
    if kind == "network_cycle":
        return "%s  CYCLE #%s %s (previous %s)" % (
            stamp, payload.get("cycle"), state, payload.get("previous_state"),
        )
    if kind == "network_transport_exhausted":
        return "%s  FALLBACK network_transport_exhausted severity=%s" % (
            stamp, payload.get("severity"),
        )
    if kind == "network_recovered":
        return "%s  RECOVERED after %s (%s)" % (
            stamp, _format_seconds(payload.get("outage_seconds") or 0.0),
            payload.get("previous_state"),
        )
    return "%s  %s" % (stamp, kind)



def build_parser() -> argparse.ArgumentParser:
    """CLI definition (kept separate so tests can assert flags without a run)."""
    parser = argparse.ArgumentParser(
        prog="netwatch",
        description="Record a link-health timeline so a suspected network cycle "
                    "can be correlated with a system log (see the module docstring).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help="node endpoint host:port to probe")
    parser.add_argument("--internet", default=DEFAULT_INTERNET,
                        help="comma-separated numeric host:port endpoints used as "
                             "the reachability probe")
    parser.add_argument("--dns-name", default=DEFAULT_DNS_NAME,
                        help="name resolved every tick; pass '' to disable")
    parser.add_argument("--gateway-port", type=int, default=None,
                        help="also probe the default gateway on this TCP port")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between samples")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="per-probe timeout in seconds")
    parser.add_argument("--duration", type=float, default=None,
                        help="stop after this many seconds (default: until interrupted)")
    parser.add_argument("--ticks", type=int, default=None,
                        help="stop after this many samples")
    parser.add_argument("--slow-ms", type=float, default=DEFAULT_SLOW_MS,
                        help="latency at or above this marks the link degraded")
    parser.add_argument("--jsonl", default=None,
                        help="append records to this file (plain JSONL, not hash-chained)")
    parser.add_argument("--quiet", action="store_true",
                        help="print only cycles, recoveries and the summary")
    return parser


def validate_args(args: argparse.Namespace) -> Optional[str]:
    """Return a usage-error message for the endpoint flags, or None if usable."""
    try:
        parse_endpoint(args.target)
    except ValueError as exc:
        return "--target: %s" % exc
    try:
        if not parse_endpoints(args.internet):
            return "--internet: no endpoints given"
    except ValueError as exc:
        return "--internet: %s" % exc
    return None


def main(argv: Optional[List[str]] = None,
         probe_factory: Optional[Callable[..., Any]] = None) -> int:
    """CLI entry point.

    ``probe_factory`` is the injection seam for tests (anything callable as
    ``factory(timeout=...)``); it defaults to :class:`NetProbe`.

    Returns ``0`` when no cycle was seen, ``1`` when at least one was and ``2``
    for a usage error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    problem = validate_args(args)
    if problem is not None:
        print("netwatch: invalid %s" % problem, file=sys.stderr)
        return 2
    factory = probe_factory if probe_factory is not None else NetProbe
    probe = factory(timeout=args.timeout)
    watcher = NetWatcher(
        probe=probe,
        dns_name=args.dns_name or None,
        target=args.target,
        internet=args.internet,
        gateway_port=args.gateway_port,
        slow_ms=args.slow_ms,
    )
    sink = JsonlWriter(args.jsonl) if args.jsonl else None

    def emit(record: Dict[str, Any]) -> None:
        if sink is not None:
            sink.write(record)
        if args.quiet and record.get("type") == "netwatch_tick":
            return
        print(format_record(record), flush=True)

    summary: Optional[Dict[str, Any]] = None
    try:
        summary = watcher.run(interval=args.interval, duration=args.duration,
                              ticks=args.ticks, on_record=emit)
    except KeyboardInterrupt:
        print("netwatch: interrupted", file=sys.stderr)
    finally:
        # Even on a clean interrupt the timeline must end with a summary and a
        # closed handle, otherwise the JSONL file has no completion marker.
        if summary is None:
            summary = watcher.summary()
        summary_record = watcher.record("netwatch_summary", summary)
        if sink is not None:
            sink.write(summary_record)
            sink.close()
    for line in watcher.summary_lines():
        print(line)
    return 1 if int(summary.get("cycles") or 0) else 0


if __name__ == "__main__":
    sys.exit(main())

