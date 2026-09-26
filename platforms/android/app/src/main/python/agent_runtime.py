"""\
ShugoCore ShugoNet agent runtime (v1.20).

A minimal TCP/JSON transport that implements the contract expected by
``shugonet_bridge.ShugonetExecutionHandler``:

    - send(peer, topic, payload)
    - query(query, peers, top_k)
    - sync(peer, since)
    - list_agents()
    - status()

Messages are newline-delimited JSON (NDJSON) over TCP. Every message is
a dict with at least ``type`` (the verb) and an id for request/response
correlation. The runtime can run as a standalone peer or embedded in a
ShugoCore agent.

Memory sharing (v1.30 dev)
--------------------------
When constructed with a ``MemoryManager`` (``memory=``), ``query`` answers
from the peer's real Tier 2 semantic memory and ``sync`` pulls/merges a
peer's Tier 2 facts into local memory with provenance (``shared_from`` /
``shared_at``) and content dedupe. Without a memory backend the runtime is
still a valid transport (it answers empty, never a fake fact).

Usage (standalone peer)::

    python3 agent_runtime.py --port 9000 --agent-id shugo-macbook

Usage (embedded in a ShugoCore agent)::

    from agent_runtime import ShugonetAgentRuntime
    runtime = ShugonetAgentRuntime(agent_id="agent-001", port=9000,
                                   memory=agent.memory)
    runtime.start()
    register_network_handlers(execution_layer, runtime)
"""

import hmac
import json
import logging
import socket
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 9000
_RECV_SIZE = 65536
_SOCKET_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 2.0  # for initial TCP handshake
_MAX_FRAME_BYTES = 1_048_576  # hard cap on one NDJSON frame (memory-DoS bound)
# Heartbeat cadence: the election's lease expires after ~30 s of silence, so a
# 10 s advertisement survives two dropped frames before a node looks dead.
_HEARTBEAT_INTERVAL = 10.0
_MAX_CLIENT_THREADS = 16  # hard cap on concurrent inbound client handlers


def _encode_frame(message: Dict[str, Any]) -> bytes:
    """Serialize one frame the way every peer expects (sorted keys + newline).

    Shared by outbound connections and by advertisements pushed down an
    *accepted* socket, so both directions speak byte-identical NDJSON.
    """
    return (json.dumps(message, sort_keys=True) + "\n").encode("utf-8")


class _PeerConnection:
    """A single TCP connection to a peer, sending NDJSON messages."""

    def __init__(self, peer_id: str, host: str, port: int):
        self.peer_id = peer_id
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        # RLock, not Lock: connect() calls close() while holding the lock, and
        # close() takes it again -- a plain Lock self-deadlocks on any real
        # add_peer()/start() connect (latent until a peer is actually dialed).
        self._lock = threading.RLock()
        self._connected = False

    def connect(self) -> bool:
        with self._lock:
            sock: Optional[socket.socket] = None
            try:
                self.close()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(_CONNECT_TIMEOUT)
                sock.connect((self.host, self.port))
                sock.settimeout(_SOCKET_TIMEOUT)
                self.sock = sock
                self._connected = True
                return True
            except Exception as exc:
                logger.warning("peer %s connect failed: %s", self.peer_id, exc)
                self._connected = False
                # Close the socket we just created: leaving it to the garbage
                # collector leaked one fd (plus a ResourceWarning storm in
                # logcat) per attempt, and the reconnect loop retries a down
                # peer every few seconds for as long as it stays down.
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                return False

    def send(self, message: Dict[str, Any]) -> bool:
        with self._lock:
            if not self._connected or self.sock is None:
                return False
            try:
                data = _encode_frame(message)
                self.sock.sendall(data)
                return True
            except Exception as exc:
                logger.warning("peer %s send failed: %s", self.peer_id, exc)
                self._connected = False
                return False

    def close(self) -> None:
        with self._lock:
            self._connected = False
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    @property
    def connected(self) -> bool:
        return self._connected
class _PeerServer(threading.Thread):
    """Background thread that accepts inbound TCP connections."""

    def __init__(self, runtime: "ShugonetAgentRuntime", host: str, port: int,
                 max_frame_bytes: int = _MAX_FRAME_BYTES,
                 auth_token: Optional[str] = None,
                 max_client_threads: int = _MAX_CLIENT_THREADS):
        super().__init__(name="shugonet-server", daemon=True)
        self._runtime = runtime
        self._host = host
        self._port = port
        self._max_frame_bytes = max(1024, int(max_frame_bytes))
        self._auth_token = str(auth_token) if auth_token else None
        self._max_client_threads = max(1, int(max_client_threads))
        self._client_slots = threading.BoundedSemaphore(self._max_client_threads)
        self._server_sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._host, self._port))
            sock.listen(5)
            sock.settimeout(1.0)
        except Exception as exc:
            logger.error("shugonet server bind failed on %s:%s: %s",
                         self._host, self._port, exc)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            # Leave _server_sock None: a socket that never bound reports
            # getsockname() port 0, and dialing port 0 fails with
            # EADDRNOTAVAIL instead of a clear "not listening".
            self._server_sock = None
            return
        self._server_sock = sock
        logger.info("shugonet server listening on %s:%d", self._host, self._port)

        while not self._stop_event.is_set():
            try:
                client_sock, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.warning("shugonet server accept error: %s", exc)
                continue
            # Shed load rather than spawning a thread per connection without
            # bound. A peer that stalls mid-request pins its handler thread
            # (the handler blocks in the memory backend), and every stalled
            # peer would otherwise leak a thread plus its fd -- unbounded
            # growth that ends in an fd/memory-exhaustion kill on a small
            # device. With bounded slots the worst case is a refused
            # connection, which the peer retries, never an unbounded leak.
            if not self._client_slots.acquire(blocking=False):
                logger.warning(
                    "shugonet server at %d concurrent clients; dropping %s",
                    self._max_client_threads, addr)
                try:
                    client_sock.close()
                except Exception:
                    pass
                continue
            try:
                client_sock.settimeout(_SOCKET_TIMEOUT)
                threading.Thread(target=self._serve_client,
                                 args=(client_sock, addr),
                                 daemon=True).start()
            except Exception as exc:
                self._client_slots.release()
                logger.warning("shugonet server spawn failed: %s", exc)
                try:
                    client_sock.close()
                except Exception:
                    pass

    def _serve_client(self, client_sock: socket.socket, addr: Any) -> None:
        """Run ``_handle_client`` while holding one concurrency slot.

        The slot is released on every exit path, so a handler that raises or
        is killed by its socket timing out can never permanently consume one
        of the bounded slots.
        """
        try:
            self._handle_client(client_sock, addr)
        finally:
            self._client_slots.release()

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except Exception:
                pass

    def _accepts(self, msg: Any) -> bool:
        """Reject anything that is not a dict with a non-empty string ``type``.

        When the runtime was created with an ``auth_token``, an inbound message
        must also carry a matching ``token`` (shared-secret gate).
        """
        if not isinstance(msg, dict):
            return False
        msg_type = msg.get("type")
        if not isinstance(msg_type, str) or not msg_type:
            return False
        if self._auth_token:
            presented = msg.get("token")
            return bool(presented) and hmac.compare_digest(
                str(presented), self._auth_token)
        return True

    def _handle_client(self, client_sock: socket.socket, addr: Any) -> None:
        buf = b""
        try:
            while not self._stop_event.is_set():
                try:
                    data = client_sock.recv(_RECV_SIZE)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                # A peer that never sends a newline must not be allowed to grow
                # our buffer without bound: close once a frame exceeds the cap.
                if len(buf) > self._max_frame_bytes and b"\n" not in buf:
                    logger.warning(
                        "shugonet frame from %s exceeds %d bytes; closing",
                        addr, self._max_frame_bytes)
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if len(line) > self._max_frame_bytes:
                        logger.warning("shugonet oversized frame from %s dropped", addr)
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except Exception as exc:
                        logger.warning("shugonet parse error: %s", exc)
                        continue
                    if not self._accepts(msg):
                        logger.warning("shugonet refused malformed message from %s", addr)
                        continue
                    try:
                        self._runtime._dispatch_message(msg, client_sock)
                    except Exception as exc:
                        logger.warning("shugonet dispatch error: %s", exc)
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

class ShugonetAgentRuntime:
    """Minimal TCP/JSON transport for ShugoNet multi-agent communication."""

    def __init__(
        self,
        agent_id: str = "shugo-peer",
        host: str = "0.0.0.0",
        port: int = 9000,
        peer_map: Optional[Dict[str, tuple]] = None,
        max_frame_bytes: int = _MAX_FRAME_BYTES,
        max_client_threads: int = _MAX_CLIENT_THREADS,
        auth_token: Optional[str] = None,
        memory: Optional[Any] = None,
        conflict_threshold: int = 20,
        conflict_window_s: float = 60.0,
        fallback_controller: Optional[Any] = None,
        reconnect_interval: float = 5.0,
        heartbeat_interval: float = _HEARTBEAT_INTERVAL,
    ):
        self.agent_id = agent_id
        self._host = host
        self._port = port
        self._max_frame_bytes = max(1024, int(max_frame_bytes))
        self._max_client_threads = max(1, int(max_client_threads))
        self._auth_token = str(auth_token) if auth_token else None
        self._started = False
        self._lock = threading.Lock()
        self._outbound: Dict[str, _PeerConnection] = {}
        self._pending: Dict[str, threading.Event] = {}
        self._pending_responses: Dict[str, Any] = {}
        self._stats: Dict[str, int] = {"sent": 0, "received": 0, "errors": 0,
                                       "imported": 0}
        self._server: Optional[_PeerServer] = None
        # Memory sharing: a MemoryManager (or any object exposing
        # export_shared_facts/import_shared_facts) makes query/sync real.
        self._memory = memory
        self._fallback_controller = fallback_controller
        self._conflict_threshold = max(1, int(conflict_threshold))
        self._conflict_window_s = max(1.0, float(conflict_window_s))
        self._conflict_events: "deque[float]" = deque()
        self._sync_watermarks: Dict[str, str] = {}
        # Bounded peer reconnection. Two nodes starting together each dial the
        # other before it is listening, so the first dial can fail; without a
        # retry the outbound socket stays down (send() and connected_peers
        # would report the peer as unreachable while it is actually up).
        self._reconnect_interval = max(0.0, float(reconnect_interval))
        self._stop_event = threading.Event()
        self._reconnect_thread: Optional[threading.Thread] = None
        # Track 1 over the mesh: the election's heartbeat advertisements ride
        # this transport too. Until now only the Android DDS layer produced
        # them, so a Python-only fleet honestly reported 'standalone' on every
        # node -- the election had no peers to compare against.
        self._heartbeat_interval = max(0.0, float(heartbeat_interval))
        self._heartbeat_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._heartbeat_handler: Optional[Callable[[Dict[str, Any]], None]] = None
        self._heartbeats: Dict[str, Dict[str, Any]] = {}
        self._heartbeat_thread: Optional[threading.Thread] = None
        if peer_map:
            for pid, (phost, pport) in peer_map.items():
                self.add_peer(pid, phost, pport)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._server = _PeerServer(self, self._host, self._port,
                                   max_frame_bytes=self._max_frame_bytes,
                                   auth_token=self._auth_token,
                                   max_client_threads=self._max_client_threads)
        self._server.start()
        for conn in list(self._outbound.values()):
            self._dial_async(conn)
        self._start_reconnect_loop()
        self._start_heartbeat_loop()

    def stop(self) -> None:
        self._started = False
        self._stop_event.set()
        self._reconnect_thread = None
        self._heartbeat_thread = None
        if self._server is not None:
            self._server.stop()
            self._server = None
        for conn in list(self._outbound.values()):
            conn.close()

    def _start_reconnect_loop(self) -> None:
        """Start the bounded peer-reconnect thread (no-op when disabled)."""
        if self._reconnect_interval <= 0:
            return
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return
        self._stop_event.clear()
        self._reconnect_thread = threading.Thread(
            name="shugonet-reconnect", target=self._reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(self._reconnect_interval):
                return
            try:
                self.reconnect_peers()
            except Exception as exc:
                logger.warning("peer reconnect pass failed: %s", exc)

    def reconnect_peers(self) -> int:
        """Re-dial every disconnected peer; return how many are now up.

        Bounded (one connect attempt per peer, each with the socket's connect
        timeout) and safe to call on demand, e.g. before reporting status.
        """
        established = 0
        for conn in list(self._outbound.values()):
            if conn.connected:
                established += 1
                continue
            try:
                if conn.connect():
                    established += 1
            except Exception as exc:
                logger.warning("peer %s reconnect failed: %s", conn.peer_id, exc)
        return established

    def add_peer(self, peer_id: str, host: str, port: int) -> None:
        conn = _PeerConnection(peer_id, host, port)
        self._outbound[peer_id] = conn
        if self._started:
            self._dial_async(conn)

    def _dial_async(self, conn: "_PeerConnection") -> None:
        """Dial one peer off the caller's thread.

        ``add_peer``/``start`` run on the agent/service init path. A peer that
        is down costs a full ``_CONNECT_TIMEOUT`` per peer there, so several
        unreachable peers stall init for seconds -- on Android that is an ANR,
        which surfaces to the user as the app dying. Dialing in the background
        keeps init responsive; ``send``/``sync`` still reconnect inline on
        demand and the reconnect loop retries, so the link is not lost by
        being lazy.
        """
        threading.Thread(
            name=f"shugonet-dial-{conn.peer_id}", daemon=True,
            target=self._dial_peer, args=(conn,)).start()

    @staticmethod
    def _dial_peer(conn: "_PeerConnection") -> None:
        try:
            conn.connect()
        except Exception as exc:
            logger.warning("peer %s connect failed: %s", conn.peer_id, exc)

    def remove_peer(self, peer_id: str) -> None:
        conn = self._outbound.pop(peer_id, None)
        if conn:
            conn.close()

    def list_agents(self) -> List[str]:
        return [self.agent_id] + list(self._outbound.keys())

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "agent_id": self.agent_id,
                "running": self._started,
                "port": self._port,
                "peers": list(self._outbound.keys()),
                "connected_peers": [pid for pid, c in self._outbound.items() if c.connected],
                "memory_enabled": self._memory is not None,
                "sync_watermarks": dict(self._sync_watermarks),
                "stats": dict(self._stats),
                "heartbeat": {
                    "interval_s": self._heartbeat_interval,
                    "advertising": self._heartbeat_provider is not None,
                    "handler": self._heartbeat_handler is not None,
                    "heard": len(self._heartbeats),
                },
            }

    # -- heartbeat / election transport --------------------------------------

    def set_heartbeat_provider(
            self, provider: Optional[Callable[[], Dict[str, Any]]]) -> None:
        """Set the callable that returns THIS node's advertisement.

        The agent wires the election's ``local_heartbeat()`` here, so the
        advertisement carries the node id, election priority and thermal state
        the rest of the fleet already reasons about.
        """
        self._heartbeat_provider = provider if callable(provider) else None

    def set_heartbeat_handler(
            self, handler: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """Set the callable invoked for every peer advertisement received."""
        self._heartbeat_handler = handler if callable(handler) else None

    def heartbeat_snapshot(self, limit: int = 16) -> Dict[str, Dict[str, Any]]:
        """Last advertisement heard per peer, bounded, for status surfaces."""
        with self._lock:
            items = sorted(self._heartbeats.items())[:max(1, int(limit))]
            return {node_id: dict(record) for node_id, record in items}

    def broadcast_heartbeat(
            self, payload: Optional[Dict[str, Any]] = None) -> int:
        """Send one advertisement to every connected peer; returns the count.

        Passing ``payload`` sends it verbatim (tests, replay); otherwise the
        provider supplies it. A missing or empty payload is skipped rather than
        broadcast, so a node without an election never advertises an identity it
        cannot back up.
        """
        if payload is None:
            provider = self._heartbeat_provider
            if provider is None:
                return 0
            try:
                payload = provider()
            except Exception as exc:
                logger.warning("heartbeat provider failed: %s", exc)
                return 0
        if not isinstance(payload, dict) or not payload:
            return 0
        # Stamp before sending: a token-gated mesh rejects any frame without the
        # shared secret, so an unstamped advertisement would be dropped by every
        # peer that runs with SHUGOCORE_MESH_TOKEN (i.e. the real fleet).
        message = self._stamp({"type": "heartbeat", "from": self.agent_id,
                               "payload": payload})
        sent = 0
        for conn in list(self._outbound.values()):
            try:
                if conn.send(message):
                    sent += 1
            except Exception as exc:
                logger.warning("heartbeat to %s failed: %s", conn.peer_id, exc)
        if sent:
            with self._lock:
                self._stats["heartbeats_sent"] = (
                    self._stats.get("heartbeats_sent", 0) + 1)
        return sent

    def _on_heartbeat_message(self, msg: Dict[str, Any]) -> None:
        """Record one inbound advertisement and hand it to the handler.

        Fire-and-forget by design: never acked (an ack every 10 s per peer is
        pure noise) and never trusted beyond its schema -- the handler (the
        election) re-validates every field it consumes.
        """
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return
        node_id = str(payload.get("node_id") or msg.get("from") or "").strip()
        if not node_id:
            return
        record = dict(payload)
        record["node_id"] = node_id
        record["received_at"] = time.monotonic()
        with self._lock:
            self._heartbeats[node_id] = record
            self._stats["heartbeats_received"] = (
                self._stats.get("heartbeats_received", 0) + 1)
        handler = self._heartbeat_handler
        seen = getattr(self, "_heartbeat_seen_nodes", None)
        if seen is None:
            seen = set()
            self._heartbeat_seen_nodes = seen
        if node_id not in seen:
            seen.add(node_id)
            logger.info("mesh heartbeat received from %s (seq=%s, handler=%s)",
                        node_id, record.get("seq"), handler is not None)
        if handler is None:
            return
        try:
            handler(record)
        except Exception as exc:
            logger.warning("heartbeat handler failed: %s", exc)

    def _start_heartbeat_loop(self) -> None:
        """Start the advertisement loop (no-op when disabled or already up)."""
        if self._heartbeat_interval <= 0:
            return
        if (self._heartbeat_thread is not None
                and self._heartbeat_thread.is_alive()):
            return
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            name="shugonet-heartbeat", target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        """Advertise on a fixed cadence until ``stop()``.

        The provider is consulted every interval instead of captured once, so
        the agent may wire the election after ``start()`` and a thermal change is
        reflected on the next beat. The *first successful* advertisement logs
        once, and an empty first cycle logs once too: a one-shot log keyed to the
        very first cycle went silent for good when the mesh came up seconds later
        than the agent, which is how a phone that was advertising perfectly well
        looked inert in the fleet logs.
        """
        announced = False
        explained = False
        while not self._stop_event.wait(self._heartbeat_interval):
            try:
                sent = self.broadcast_heartbeat()
            except Exception as exc:
                logger.warning("heartbeat broadcast failed: %s", exc)
                continue
            if sent:
                if not announced:
                    announced = True
                    logger.info("mesh heartbeat: advertising to %d peer(s) "
                                "every %.0fs", sent, self._heartbeat_interval)
            elif not explained:
                explained = True
                logger.info("mesh heartbeat: nothing to advertise yet "
                            "(provider=%s, peers=%d)",
                            self._heartbeat_provider is not None,
                            len(self._outbound))

    # -- memory sharing ------------------------------------------------------

    def _memory_search(self, query: str,
                       top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        """Answer a peer query from real Tier 2 memory (empty if none)."""
        memory = self._memory
        if memory is None or not query:
            return []
        limit = max(1, int(top_k)) if top_k else 5
        try:
            if hasattr(memory, "retrieve_context"):
                hits = memory.retrieve_context(query, top_k=limit,
                                               include_episodic=False)
                facts = list(hits.get("semantic", [])) + list(hits.get("graph", []))
            elif hasattr(memory, "search"):
                facts = memory.search(query, top_k=limit)
            else:
                return []
        except Exception as exc:
            logger.warning("shugonet memory search failed: %s", exc)
            return []
        results = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            content = fact.get("content")
            if not content:
                continue
            metadata = fact.get("metadata") or {}
            results.append({
                "fact": content,
                "salience": fact.get("salience", 1.0),
                "kind": fact.get("kind", "fact"),
                # origin: the peer this fact was learned from (provenance), so
                # a caller can tell its own knowledge from mesh-imported facts.
                "origin": metadata.get("shared_from") or self.agent_id,
                "source": self.agent_id,
            })
        return results

    def _memory_export(self, since: Optional[str]) -> List[Dict[str, Any]]:
        """Export local Tier 2 facts created after ``since`` (or [] if none)."""
        memory = self._memory
        if memory is None:
            return []
        exporter = getattr(memory, "export_shared_facts", None)
        if exporter is None:
            return []
        try:
            return exporter(since)
        except Exception as exc:
            logger.warning("shugonet memory export failed: %s", exc)
            return []

    def _memory_import(self, facts: List[Dict[str, Any]],
                       source: str) -> Dict[str, int]:
        """Merge peer facts into local memory (no-op without a backend)."""
        memory = self._memory
        if memory is None:
            return {"imported": 0, "skipped": 0, "duplicates": 0}
        importer = getattr(memory, "import_shared_facts", None)
        if importer is None:
            return {"imported": 0, "skipped": 0, "duplicates": 0}
        try:
            return importer(facts, source)
        except Exception as exc:
            logger.warning("shugonet memory import failed: %s", exc)
            return {"imported": 0, "skipped": 0, "duplicates": 0}

    def _note_conflicts(self, source: str, duplicates: int) -> None:
        """Guard against a peer storming us with facts we already hold.

        Duplicate content is idempotent (never duplicated), but a sustained
        rate of duplicates means a peer is replaying its whole store. When
        that exceeds ``conflict_threshold`` within ``conflict_window_s`` the
        deterministic ``memory_sync_conflict_storm`` fallback fires.
        """
        if duplicates <= 0:
            return
        now = time.monotonic()
        with self._lock:
            for _ in range(duplicates):
                self._conflict_events.append(now)
            cutoff = now - self._conflict_window_s
            while self._conflict_events and self._conflict_events[0] < cutoff:
                self._conflict_events.popleft()
            storm = len(self._conflict_events) >= self._conflict_threshold
        if storm and self._fallback_controller is not None:
            try:
                self._fallback_controller.report_violation(
                    "memory_sync_conflict_storm",
                    f"{self._conflict_threshold}+ duplicate facts within "
                    f"{self._conflict_window_s:.0f}s from '{source}'")
            except Exception as exc:
                logger.warning("conflict-storm report failed: %s", exc)

    def _stamp(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Attach the shared-secret token when this runtime gates inbound."""
        if self._auth_token:
            msg["token"] = self._auth_token
        return msg

    @staticmethod
    def _peer_error(resp: Any) -> Optional[str]:
        """Return a peer's error reason when ``resp`` is an error frame.

        Peers answer ``query``/``sync`` with a typed result frame. A peer that
        refuses -- a protocol/version mismatch, a rejected token, an unknown
        verb -- answers with an ``error`` frame carrying ``message``/``reason``
        instead. Such a reply is a *truthy dict*, so without this check the
        caller reads it as a successful transfer of zero facts and reports
        ``status: success`` while the peer actually refused. Never let a
        refusal masquerade as an empty success.
        """
        if not isinstance(resp, dict):
            return "malformed peer reply"
        if resp.get("type") == "error" or resp.get("status") == "error":
            reason = resp.get("message") or resp.get("reason") or "peer reported error"
            return str(reason)[:200]
        return None

    def send(self, peer: str, topic: str, payload: Any) -> Dict[str, Any]:
        conn = self._outbound.get(peer)
        if conn is None:
            return {"status": "refused", "reason": f"unknown peer '{peer}'"}
        if not conn.connected:
            conn.connect()
            if not conn.connected:
                return {"status": "refused", "reason": f"peer '{peer}' not reachable"}
        msg = self._stamp({"type": "send", "from": self.agent_id, "topic": topic,
                           "payload": payload, "id": str(uuid.uuid4().hex[:12])})
        ok = conn.send(msg)
        if ok:
            self._stats["sent"] += 1
            return {"status": "success", "via": "tcp", "peer": peer}
        return {"status": "error", "message": "send failed"}

    def _one_shot_request(self, conn: "_PeerConnection", msg: Dict[str, Any],
                          timeout: float = _SOCKET_TIMEOUT
                          ) -> Optional[Dict[str, Any]]:
        """Send one request and read one reply on a dedicated connection.

        Request/response deliberately does NOT use the persistent outbound
        socket: a peer answers on the connection the request arrived on, so a
        fresh short-lived socket gives a deterministic one-to-one reply with
        no background reader thread and no shared-socket races.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((conn.host, conn.port))
        except Exception as exc:
            logger.warning("peer %s request connect failed: %s", conn.peer_id, exc)
            return None
        try:
            payload = json.dumps(self._stamp(msg), sort_keys=True) + "\n"
            sock.sendall(payload.encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(_RECV_SIZE)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > self._max_frame_bytes:
                    break
            line = buf.split(b"\n", 1)[0].strip()
            if not line:
                return None
            return json.loads(line.decode("utf-8"))
        except Exception as exc:
            logger.warning("peer %s request failed: %s", conn.peer_id, exc)
            return None
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def query(self, query: str, peers=None, top_k=None) -> List[Dict[str, Any]]:
        """Ask peers for facts matching ``query`` (real Tier 2 on each peer).

        Returns the raw ``query_result`` replies, one per reachable peer.
        """
        targets = peers if peers else list(self._outbound.keys())
        results = []
        for pid in targets:
            conn = self._outbound.get(pid)
            if conn is None:
                continue
            qid = str(uuid.uuid4().hex[:12])
            resp = self._one_shot_request(conn, {
                "type": "query", "from": self.agent_id, "query": query,
                "top_k": top_k, "id": qid})
            if resp:
                peer_error = self._peer_error(resp)
                if peer_error is not None:
                    self._stats["errors"] = self._stats.get("errors", 0) + 1
                    logger.warning("peer %s refused query: %s", pid, peer_error)
                    continue
                self._stats["sent"] += 1
                results.append(resp)
        return results

    def sync(self, peer=None, since=None) -> Dict[str, Any]:
        """Pull a peer's Tier 2 facts since ``since`` and merge them locally.

        Returns ``{status, peer, received, imported, duplicates}``. Without a
        memory backend the transfer still succeeds but imports nothing.
        """
        pid = peer or next(iter(self._outbound.keys()), None)
        if pid is None:
            return {"status": "refused", "reason": "no peers available"}
        conn = self._outbound.get(pid)
        if conn is None:
            return {"status": "refused", "reason": f"unknown peer '{pid}'"}
        watermark = since if since is not None else self._sync_watermarks.get(pid)
        qid = str(uuid.uuid4().hex[:12])
        resp = self._one_shot_request(conn, {
            "type": "sync", "from": self.agent_id,
            "since": watermark, "id": qid})
        if not resp:
            return {"status": "error", "message": "sync unreachable",
                    "peer": pid}
        peer_error = self._peer_error(resp)
        if peer_error is not None:
            self._stats["errors"] = self._stats.get("errors", 0) + 1
            logger.warning("peer %s refused sync: %s", pid, peer_error)
            return {"status": "error", "peer": pid, "message": peer_error,
                    "received": 0, "imported": 0, "duplicates": 0}
        self._stats["sent"] += 1
        facts = resp.get("facts") or []
        merge = self._memory_import(facts, pid)
        self._note_conflicts(pid, int(merge.get("duplicates", 0)))
        imported = int(merge.get("imported", 0))
        self._stats["imported"] = self._stats.get("imported", 0) + imported
        if facts:
            newest = facts[-1].get("created_at") if isinstance(facts[-1], dict) else None
            if newest:
                self._sync_watermarks[pid] = newest
        return {"status": "success", "peer": pid, "received": len(facts),
                "imported": imported,
                "duplicates": int(merge.get("duplicates", 0))}

    def _dispatch_message(self, msg: Dict[str, Any], sock: socket.socket) -> None:
        self._stats["received"] += 1
        msg_type = msg.get("type")
        msg_id = msg.get("id", "")
        if msg_type == "send":
            ack = {"type": "ack", "in_response_to": msg_id, "status": "received"}
            self._send_json(sock, ack)
        elif msg_type == "query":
            resp = {"type": "query_result", "in_response_to": msg_id,
                    "from": self.agent_id,
                    "query": msg.get("query", ""),
                    "results": self._memory_search(msg.get("query", ""),
                                                   msg.get("top_k"))}
            self._send_json(sock, resp)
        elif msg_type == "sync":
            facts = self._memory_export(msg.get("since"))
            resp = {"type": "sync_result", "in_response_to": msg_id,
                    "from": self.agent_id, "since": msg.get("since"),
                    "facts": facts, "count": len(facts)}
            self._send_json(sock, resp)
        elif msg_type == "heartbeat":
            # Track 1 advertisement: consumed, never acked.
            self._on_heartbeat_message(msg)
        elif msg_type in ("ack", "query_result", "sync_result"):
            with self._lock:
                rid = msg.get("in_response_to", "")
                if rid in self._pending:
                    self._pending_responses[rid] = msg
                    self._pending[rid].set()

    def _send_json(self, sock: socket.socket, msg: Dict[str, Any]) -> None:
        try:
            data = (json.dumps(msg, sort_keys=True) + "\n").encode("utf-8")
            sock.sendall(data)
        except Exception:
            pass

    def _recv_response(self, qid: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        evt = threading.Event()
        with self._lock:
            self._pending[qid] = evt
        evt.wait(timeout=timeout)
        with self._lock:
            self._pending.pop(qid, None)
            return self._pending_responses.pop(qid, None)


def main() -> None:
    import argparse
    import os
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="ShugoNet peer runtime")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--agent-id", default="shugo-peer")
    parser.add_argument("--peer", action="append", nargs=3, metavar=("ID", "HOST", "PORT"))
    parser.add_argument("--auth-token-env", default="SHUGOCORE_MESH_TOKEN",
                        help="env var holding the mesh shared secret (optional)")
    parser.add_argument("--max-frame-bytes", type=int, default=_MAX_FRAME_BYTES,
                        help="max NDJSON frame size in bytes (default: 1048576)")
    args = parser.parse_args()
    peer_map = {}
    if args.peer:
        for pid, host, port in args.peer:
            peer_map[pid] = (host, int(port))
    token = os.environ.get(args.auth_token_env) or None
    runtime = ShugonetAgentRuntime(agent_id=args.agent_id, port=args.port,
                                   peer_map=peer_map or None,
                                   max_frame_bytes=args.max_frame_bytes,
                                   auth_token=token)
    runtime.start()
    logger.info("ShugoNet peer '%s' running on port %d", args.agent_id, args.port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runtime.stop()


if __name__ == "__main__":
    main()
