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

Usage (standalone peer)::

    python3 agent_runtime.py --port 9000 --agent-id shugo-macbook

Usage (embedded in a ShugoCore agent)::

    from agent_runtime import ShugonetAgentRuntime
    runtime = ShugonetAgentRuntime(agent_id="agent-001", port=9000)
    runtime.start()
    register_network_handlers(execution_layer, runtime)
"""

import json
import logging
import socket
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 9000
_RECV_SIZE = 65536
_SOCKET_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 2.0  # for initial TCP handshake


class _PeerConnection:
    """A single TCP connection to a peer, sending NDJSON messages."""

    def __init__(self, peer_id: str, host: str, port: int):
        self.peer_id = peer_id
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._connected = False

    def connect(self) -> bool:
        with self._lock:
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
                return False

    def send(self, message: Dict[str, Any]) -> bool:
        with self._lock:
            if not self._connected or self.sock is None:
                return False
            try:
                data = (json.dumps(message, sort_keys=True) + "\n").encode("utf-8")
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

    def __init__(self, runtime: "ShugonetAgentRuntime", host: str, port: int):
        super().__init__(name="shugonet-server", daemon=True)
        self._runtime = runtime
        self._host = host
        self._port = port
        self._server_sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind((self._host, self._port))
            self._server_sock.listen(5)
            self._server_sock.settimeout(1.0)
            logger.info("shugonet server listening on %s:%d", self._host, self._port)
        except Exception as exc:
            logger.error("shugonet server bind failed: %s", exc)
            return

        while not self._stop_event.is_set():
            try:
                client_sock, addr = self._server_sock.accept()
                client_sock.settimeout(_SOCKET_TIMEOUT)
                t = threading.Thread(
                    target=self._handle_client, args=(client_sock, addr), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.warning("shugonet server accept error: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except Exception:
                pass

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
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                        self._runtime._dispatch_message(msg, client_sock)
                    except Exception as exc:
                        logger.warning("shugonet parse error: %s", exc)
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
    ):
        self.agent_id = agent_id
        self._host = host
        self._port = port
        self._started = False
        self._lock = threading.Lock()
        self._outbound: Dict[str, _PeerConnection] = {}
        self._pending: Dict[str, threading.Event] = {}
        self._pending_responses: Dict[str, Any] = {}
        self._stats: Dict[str, int] = {"sent": 0, "received": 0, "errors": 0}
        self._server: Optional[_PeerServer] = None
        if peer_map:
            for pid, (phost, pport) in peer_map.items():
                self.add_peer(pid, phost, pport)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._server = _PeerServer(self, self._host, self._port)
        self._server.start()
        for conn in list(self._outbound.values()):
            conn.connect()

    def stop(self) -> None:
        self._started = False
        if self._server is not None:
            self._server.stop()
            self._server = None
        for conn in list(self._outbound.values()):
            conn.close()

    def add_peer(self, peer_id: str, host: str, port: int) -> None:
        conn = _PeerConnection(peer_id, host, port)
        self._outbound[peer_id] = conn
        if self._started:
            conn.connect()

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
                "stats": dict(self._stats),
            }

    def send(self, peer: str, topic: str, payload: Any) -> Dict[str, Any]:
        conn = self._outbound.get(peer)
        if conn is None:
            return {"status": "refused", "reason": f"unknown peer '{peer}'"}
        if not conn.connected:
            conn.connect()
            if not conn.connected:
                return {"status": "refused", "reason": f"peer '{peer}' not reachable"}
        msg = {"type": "send", "from": self.agent_id, "topic": topic,
               "payload": payload, "id": str(uuid.uuid4().hex[:12])}
        ok = conn.send(msg)
        if ok:
            self._stats["sent"] += 1
            return {"status": "success", "via": "tcp", "peer": peer}
        return {"status": "error", "message": "send failed"}

    def query(self, query: str, peers=None, top_k=None) -> List[Dict[str, Any]]:
        targets = peers if peers else list(self._outbound.keys())
        results = []
        for pid in targets:
            conn = self._outbound.get(pid)
            if conn is None or not conn.connected:
                continue
            qid = str(uuid.uuid4().hex[:12])
            msg = {"type": "query", "from": self.agent_id, "query": query,
                   "top_k": top_k, "id": qid}
            ok = conn.send(msg)
            if ok:
                self._stats["sent"] += 1
                resp = self._recv_response(qid, timeout=2.0)
                if resp:
                    results.append(resp)
        return results

    def sync(self, peer=None, since=None) -> Dict[str, Any]:
        pid = peer or next(iter(self._outbound.keys()), None)
        if pid is None:
            return {"status": "refused", "reason": "no peers available"}
        conn = self._outbound.get(pid)
        if conn is None or not conn.connected:
            return {"status": "refused", "reason": f"peer '{pid}' not connected"}
        msg = {"type": "sync", "from": self.agent_id, "since": since,
               "id": str(uuid.uuid4().hex[:12])}
        ok = conn.send(msg)
        if ok:
            self._stats["sent"] += 1
            return {"status": "success", "peer": pid}
        return {"status": "error", "message": "sync failed"}

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
                    "results": [{"fact": f"stub from {self.agent_id}", "salience": 0.5}]}
            self._send_json(sock, resp)
        elif msg_type == "sync":
            ack = {"type": "ack", "in_response_to": msg_id, "status": "synced"}
            self._send_json(sock, ack)
        elif msg_type in ("ack", "query_result"):
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="ShugoNet peer runtime")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--agent-id", default="shugo-peer")
    parser.add_argument("--peer", action="append", nargs=3, metavar=("ID", "HOST", "PORT"))
    args = parser.parse_args()
    peer_map = {}
    if args.peer:
        for pid, host, port in args.peer:
            peer_map[pid] = (host, int(port))
    runtime = ShugonetAgentRuntime(agent_id=args.agent_id, port=args.port, peer_map=peer_map or None)
    runtime.start()
    logger.info("ShugoNet peer '%s' running on port %d", args.agent_id, args.port)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runtime.stop()


if __name__ == "__main__":
    main()
