#!/usr/bin/env python3
"""Prove the fleet's dev-task channel: a hub names a task, a peer runs it, the hive records it.

Two real nodes, one process each, over loopback and the fleet's own transport:

    1. a peer node (``runtime/tools/devlab_node.py``) with its own data dir and identity;
    2. this node, the hub, at the fleet's hub priority, holding the lease;
    3. the operator path an operator actually uses -- consent granted, approval broker
       attached, the action run through the engine's gate (never around it);
    4. one named task, delegated to the peer, which resolves the name against *its* registry,
       runs it with ``shell=False``, audits both ends and reports back.

The transcript (``runtime/evidence/dev_task.txt``) carries lines produced by the *peer's*
process -- its argv, its exit code, its audit entries -- so the claim does not rest on the
hub's account of what happened.

    python runtime/tools/dev_task_proof.py --task node_consistency

Nothing in the request is an argv: the wire payload is printed so that is checkable, and
``tests/test_dev_tasks.py`` holds the registry to the same rule.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CLIENTS = ROOT / "clients" / "desktop"
if str(CLIENTS) not in sys.path:
    sys.path.insert(0, str(CLIENTS))

DEFAULT_TASK = "node_consistency"
DEFAULT_OUT = os.path.join("runtime", "evidence", "dev_task.txt")
PEER_KEYS = ("[devlab]", "[devlab-log]", "requested by")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", default=DEFAULT_TASK,
                    help="the name the hub will send (a name, never an argv)")
    ap.add_argument("--peer", default="", help="requested peer id (default: the fleet's best)")
    ap.add_argument("--hub-port", type=int, default=9000)
    ap.add_argument("--peer-port", type=int, default=9011,
                    help="the lab peer's port: off the hub's, and off 9001, which is the "
                         "relay/HTTP port this mesh also uses")
    ap.add_argument("--peer-caps", default="devlab")
    ap.add_argument("--peer-priority", type=int, default=500)
    ap.add_argument("--hub-data-dir", default=os.path.join("runtime", "desktop"))
    ap.add_argument("--lab-dir", default=os.path.join("runtime", "devlab"))
    ap.add_argument("--seconds", type=float, default=45.0,
                    help="how long to wait for the peer to appear and report back")
    ap.add_argument("--consent-ttl", type=float, default=600.0)
    ap.add_argument("--out", default=DEFAULT_OUT)
    return ap.parse_args(argv)


def peer_identity(caps: str) -> str:
    """The id a node takes from its capability string -- the same the fleet sees."""
    return f"shugo-{caps}"


def prepare_lab(args) -> str:
    """Give the peer its own data dir and the fleet's token, like a real device.

    The token file is copied rather than generated: a node with a different secret advertises
    into a wall, and every peer refuses its frames -- which would look like "the peer ignored
    the task" instead of "the peer was never in the hive".
    """
    lab = Path(args.lab_dir)
    lab.mkdir(parents=True, exist_ok=True)
    token = ""
    source = Path(args.hub_data_dir) / "mesh_token.txt"
    try:
        token = source.read_text(encoding="utf-8").strip().split()[0][:256]
    except Exception:
        token = ""
    if token:
        (lab / "mesh_token.txt").write_text(token + "\n", encoding="utf-8")
    return token


class PeerProcess:
    """The peer node as a subprocess, with its output captured line by line."""

    def __init__(self, args, token: str):
        self.args = args
        self.token = token
        self.lines = []
        self.process = None
        self._thread = None

    def start(self) -> None:
        argv = [sys.executable, os.path.join("runtime", "tools", "devlab_node.py"),
                "--port", str(self.args.peer_port), "--device-caps", self.args.peer_caps,
                "--priority", str(self.args.peer_priority),
                "--data-dir", self.args.lab_dir,
                "--peers", f"{peer_identity('desktop')}=127.0.0.1:{self.args.hub_port}",
                "--seconds", str(max(30.0, self.args.seconds + 30.0))]
        if self.token:
            argv += ["--token", self.token]
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(  # noqa: S603 - our own tool, vector args, no shell
            argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, shell=False)
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        stream = self.process.stdout if self.process else None
        if stream is None:
            return
        for line in stream:
            text = line.rstrip()
            if not text:
                continue
            self.lines.append(text)
            if any(key in text for key in PEER_KEYS):
                print(f"  [peer] {text}", flush=True)

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass

    def matching(self, *keys) -> list:
        """The peer's own lines mentioning any of ``keys``."""
        return [line for line in list(self.lines)
                if any(key in line for key in keys)]

    def ran_line(self) -> str:
        """The peer's own line about running a task, or ''."""
        for line in self.matching("requested by"):
            return line
        return ""


def _hub_mesh(controller) -> dict:
    """The hub's own view of the mesh: who it will dial, and who answered."""
    try:
        snapshot = controller.snapshot() or {}
    except Exception:
        return {}
    mesh = snapshot.get("mesh") or {}
    return {"node": mesh.get("node_id"), "port": mesh.get("port"),
            "configured_peers": mesh.get("configured_peers"),
            "connected_peers": mesh.get("connected_peers"),
            "stats": mesh.get("stats"), "heartbeat": mesh.get("heartbeat")}


def _mesh_logs(controller, limit: int = 12) -> list:
    """The hub's own MESH/FLEET log lines -- what the sending node observed."""
    try:
        snapshot = controller.snapshot() or {}
    except Exception:
        return []
    lines = []
    for entry in (snapshot.get("logs") or []):
        category = str(entry.get("category") or "")
        if category in ("MESH", "FLEET"):
            lines.append(f"[{category}] {entry.get('message')}")
    return lines[-limit:]


def _delegated_records(agent) -> list:
    try:
        status = agent.get_status() or {}
    except Exception:
        status = {}
    delegated = status.get("delegated") if isinstance(status, dict) else None
    records = (delegated or {}).get("results") if isinstance(delegated, dict) else None
    return list(records or [])


def _wire_payload(result) -> dict:
    """The payload the hub actually handed the mesh, from the handler's own return."""
    for candidate in (result, (result or {}).get("result"),
                      (result or {}).get("handler_result")):
        if isinstance(candidate, dict) and isinstance(candidate.get("wire"), dict):
            return dict(candidate["wire"])
    return {}


def run_proof(args) -> dict:
    """Bring up the peer and the hub, name one task, and collect what happened."""
    import logging

    import shugocore_desktop as console

    peer_id = peer_identity(args.peer_caps)
    facts = {"task": args.task, "requested_peer": args.peer, "peer": peer_id,
             "hub": peer_identity("desktop"), "consent": "", "approval": "",
             "engine": {}, "wire": {}, "result": {}, "hub_role": "", "peers": [],
             "lease": "", "peer_lines": [], "token": False, "error": "",
             "peer_priority": args.peer_priority}
    token = prepare_lab(args)
    facts["token"] = bool(token)
    peer = PeerProcess(args, token)
    controller = None
    started = time.time()
    try:
        peer.start()
        # Wait for the peer's own "up" line before the hub looks for it: a heartbeat with
        # nobody behind it is a race, not a mesh.
        for _ in range(60):
            if peer.matching("[devlab] node "):
                break
            time.sleep(0.5)
        # The hub's own peers come from the environment, so the desk node's mesh_peers.json
        # (the real fleet's file) is never rewritten by a proof run.
        existing = os.environ.get("SHUGOCORE_MESH_PEERS", "").strip(", ")
        entry = f"{peer_id}=127.0.0.1:{args.peer_port}"
        os.environ["SHUGOCORE_MESH_PEERS"] = f"{existing},{entry}".strip(",")
        spec = console.backend_by_label("Stub (offline)")
        controller = console.AgentController(interval=1.0, sync_interval=0.0)
        error = controller.start(spec, spec.get("url", ""), "",
                                 data_dir=args.hub_data_dir, device_caps="desktop",
                                 mesh={"port": args.hub_port, "peers": "", "priority": 1,
                                       "token": ""}) or ""
        logging.getLogger().setLevel(logging.WARNING)
        if error:
            facts["error"] = f"hub did not start: {error}"
            return facts
        agent = controller.agent
        deadline = time.monotonic() + max(15.0, args.seconds)
        while time.monotonic() < deadline:
            status = agent.get_status() or {}
            facts["hub_role"] = str(status.get("mesh_role") or "")
            facts["peers"] = [str((p or {}).get("device_id") or "")
                              for p in ((agent.telemetry or {}).get("mesh_peers") or [])]
            try:
                facts["lease"] = str(agent.mesh_election.primary() or "")
            except Exception:
                facts["lease"] = ""
            if peer_id in facts["peers"] and facts["lease"] == facts["hub"]:
                break
            time.sleep(1.0)
        facts["waited_s"] = round(time.time() - started, 1)
        engine = getattr(agent, "engine", None)
        if engine is None:
            facts["error"] = "the hub has no engine to gate the request"
            return facts
        # The operator path, not a shortcut around it: consent first, an approval channel
        # second, and the action through the engine's own gate.
        grant = engine.consents.grant("fleet_dev_task",
                                      granted_by="dev_task_proof (operator console)",
                                      note=f"one {args.task} on {peer_id}",
                                      ttl_seconds=args.consent_ttl)
        facts["consent"] = (f"granted '{grant['action_type']}' by {grant['granted_by']} "
                            f"(ttl {args.consent_ttl:.0f}s)")
        requests = []

        def operator(request):
            requests.append(dict(request or {}))
            return True

        engine.approvals.attach_operator(operator)
        # A proof run names the lab peer it just started. Letting the hub pick would send real
        # work to the real fleet -- measured live, the ranking chose the Mac (priority 10)
        # over this peer (500) -- which is the right behaviour for a hive and the wrong thing
        # to do to the operator's machines to make a point.
        chosen = str(args.peer or peer_id)
        facts["named_peer"] = chosen
        params = {"task": args.task, "peer": chosen}
        # The engine's own decision gate, not a shortcut past it: ``execute_task`` is the
        # *planner* (it asks the model to propose a decision), so an operator console names the
        # decision and runs it through the same two steps the planner uses -- consent, then
        # approval, then execute -- which is why both gates appear in this transcript.
        decision = {"action_type": "fleet_dev_task", "params": params, "confidence": 1.0,
                    "decision_id": "dev-task-proof"}
        facts["gate"] = ("DecisionEngine._gate_decision (consent + approval) -> "
                         "_execute_gated: the pair the planner runs internally")
        allowed, reason = engine._gate_decision(decision, None)
        if not allowed:
            facts["engine"] = {"status": "refused", "reason": str(reason or "gated")}
            facts["error"] = f"the gate refused the dev task: {reason}"
            return facts
        result = engine._execute_gated(decision) or {}
        facts["engine"] = {"status": str(result.get("status") or ""),
                           "outcome": str(result.get("outcome") or ""),
                           "reason": str(result.get("reason") or "")[:200],
                           "route": str(result.get("route") or ""),
                           "peer": str(result.get("peer") or ""),
                           "task": str(result.get("task") or ""),
                           "message": str(result.get("message") or "")[:200],
                           "keys": ",".join(sorted(str(key) for key in result))[:200],
                           "registered": "fleet_dev_task" in (
                               getattr(engine.execution_layer, "_handlers", {}) or {})}
        facts["wire"] = _wire_payload(result)
        facts["approval"] = (f"operator channel attached; requested "
                             f"{len(requests)} approval(s) -> granted")
        deadline = time.monotonic() + max(15.0, args.seconds)
        while time.monotonic() < deadline:
            records = [record for record in _delegated_records(agent)
                       if str(record.get("action_type")) == "dev_task"]
            if records:
                facts["result"] = dict(records[-1])
                break
            time.sleep(1.0)
        facts["hub_logs"] = _mesh_logs(controller)
        facts["hub_mesh"] = _hub_mesh(controller)
        return facts
    except Exception as exc:
        facts["error"] = f"{type(exc).__name__}: {exc}"
        return facts
    finally:
        if controller is not None:
            try:
                controller.stop()
            except Exception:
                pass
        peer.stop()
        time.sleep(1.0)                     # let the drain thread take the last lines
        facts["peer_lines"] = list(peer.lines)


def _exit_from(line: str) -> str:
    match = re.search(r"exit=(-?\d+)", str(line or ""))
    return match.group(1) if match else "?"


def transcript_lines(facts: dict) -> list:
    """The proof as lines, with the peer's own output quoted and labelled as the peer's.

    Line order matters to the parser: the wire payload is stated before the outcome, so the
    "a name, not an argv" claim is visible next to what was actually sent.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    peers = [name for name in (facts.get("peers") or []) if name]
    ran = ""
    for line in facts.get("peer_lines") or []:
        if "requested by" in line:
            ran = line
            break
    result = facts.get("result") or {}
    lines = [
        f"dev task proof: {stamp}  hub={facts.get('hub')} (priority 1)  "
        f"peer={facts.get('peer')} (priority {facts.get('peer_priority')})",
        f"fleet: hub role={facts.get('hub_role') or 'unknown'}  "
        f"lease={facts.get('lease') or 'nobody'}  peers={len(peers)} "
        f"({', '.join(peers) or 'none heard'})",
        "token: " + ("the fleet token was copied into the peer's data dir"
                     if facts.get("token") else "no fleet token exists (open mesh)"),
    ]
    if facts.get("error"):
        lines.append(f"error={facts['error']}")
    if facts.get("consent"):
        lines.append(f"consent: {facts['consent']}")
    if facts.get("approval"):
        lines.append(f"approval: {facts['approval']}")
    if facts.get("gate"):
        lines.append(f"gate: {facts['gate']}")
    engine = facts.get("engine") or {}
    if engine:
        lines.append(f"hub named the task: action_type=fleet_dev_task "
                     f"task={facts.get('task')} -> status={engine.get('status')} "
                     f"task={engine.get('task')} peer={engine.get('peer')} "
                     f"route={engine.get('route')}")
        lines.append(f"engine detail: handler_registered={engine.get('registered')} "
                     f"message={engine.get('message')} keys={engine.get('keys')}")
    if facts.get("wire"):
        lines.append("wire payload (a name, not an argv): "
                     + json.dumps(facts["wire"], sort_keys=True))
    if facts.get("named_peer"):
        lines.append(f"request: task={facts.get('task')} peer={facts['named_peer']} "
                     f"(named explicitly; the hub can also rank the fleet itself)")
    if ran:
        lines.append(f"peer ran: {ran}   <- the peer's own process said this")
    for line in facts.get("peer_lines") or []:
        if "[devlab-audit]" in line:
            lines.append("peer audit: "
                         + line.split("[devlab-audit]", 1)[1].strip())
        elif "[devlab-log]" in line:
            lines.append("peer log: " + line.split("[devlab-log]", 1)[1].strip())
    for line in facts.get("hub_logs") or []:
        lines.append("hub log: " + str(line))
    if facts.get("hub_mesh"):
        lines.append("hub mesh: " + json.dumps(facts["hub_mesh"], sort_keys=True))
    if result:
        lines.append("result from peer: status={0} action_type={1} delivered={2} "
                     "reason={3}".format(result.get("status"),
                                         result.get("action_type"),
                                         result.get("delivered"),
                                         str(result.get("reason") or "")[:160]))
    lines.append(f"verdict: task={facts.get('task')} peer={facts.get('peer')} "
                 f"exit={_exit_from(ran)} delivered={result.get('delivered')} "
                 f"ran_by={facts.get('peer') if ran else 'nobody'}")
    return lines


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        facts = run_proof(args)
    except Exception as exc:                # pragma: no cover - run_proof guards its own
        facts = {"task": args.task, "peer": peer_identity(args.peer_caps),
                 "hub": peer_identity("desktop"), "peer_priority": args.peer_priority,
                 "error": f"{type(exc).__name__}: {exc}"}
    lines = transcript_lines(facts)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The peer's raw output, kept next to the transcript: the transcript quotes it, and the
    # whole point of the peer being a separate process is that its own words survive.
    if facts.get("peer_lines"):
        Path(os.path.dirname(args.out) or ".").joinpath("devlab_node.log").write_text(
            "\n".join(facts["peer_lines"]) + "\n", encoding="utf-8")
    for line in lines:
        print(line, flush=True)
    return 1 if facts.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
