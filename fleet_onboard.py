#!/usr/bin/env python3
"""Onboard a node into the hive: the two things it cannot work out for itself.

A node is accepted into the mesh only if it presents the fleet's shared secret, and it
reaches anyone only if it knows which peers to dial. Both are device-local files --
``mesh_token.txt`` and ``mesh_peers.json`` -- so joining is a deliberate act with two
inputs, one of which must be identical across the fleet.

    python3 fleet_onboard.py --data-dir runtime/desktop --node-id shugo-desktop \
        --peers "shugo-tab=192.168.1.164:9000,shugo-a16=192.168.1.176:9000" \
        --token-file runtime/desktop/mesh_token.txt

It is idempotent, and it refuses to fork the fleet: regenerating a token makes every frame
in the hive refused, so an existing secret is reused, a ``--token`` that disagrees with the
file on disk is an error rather than an overwrite, and peers are merged rather than
replaced (onboarding one node must not forget the others).

``--check`` reads a node's directory and changes nothing -- the thing to run before blaming
the mesh for what is really a bad token.
"""
import argparse
import json
import os
import secrets

TOKEN_NAME = "mesh_token.txt"
PEERS_NAME = "mesh_peers.json"
TOKEN_CHARS = 32


def parse_peers(spec: str):
    """Parse ``"id=host:port,id2=host:port"`` into (peers, problems).

    Malformed entries are reported, never dropped silently: a typo in a peer list is a
    node that is present in the file and absent from the mesh.
    """
    peers, problems = [], []
    for entry in str(spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        peer_id, sep, address = entry.partition("=")
        host, _, port_text = address.rpartition(":")
        peer_id, host = peer_id.strip(), host.strip()
        try:
            port = int(port_text)
        except (TypeError, ValueError):
            port = 0
        if not (sep and peer_id and host and 0 < port < 65536):
            problems.append(entry)
            continue
        peers.append({"id": peer_id, "host": host, "port": port})
    return peers, problems


def read_peers_file(path: str):
    """Read either accepted form: ``{id: "host:port"}`` or ``[{id, host, port}]``."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError) as exc:
        return [], [f"{PEERS_NAME} unreadable: {exc}"]
    if isinstance(data, dict):
        entries = [{"id": key, "address": value} for key, value in data.items()]
    elif isinstance(data, list):
        entries = [entry for entry in data if isinstance(entry, dict)]
    else:
        return [], [f"{PEERS_NAME} is neither an object nor a list"]
    peers, problems = [], []
    for entry in entries:
        if "address" in entry:
            host, _, port_text = str(entry.get("address") or "").rpartition(":")
        else:
            host, port_text = str(entry.get("host") or ""), str(entry.get("port") or "")
        peer_id = str(entry.get("id") or "").strip()
        try:
            port = int(port_text)
        except (TypeError, ValueError):
            port = 0
        if not (peer_id and host.strip() and 0 < port < 65536):
            problems.append(f"{peer_id or '?'}: malformed peer entry")
            continue
        peers.append({"id": peer_id, "host": host.strip(), "port": port})
    return peers, problems


def write_peers_file(path: str, peers) -> None:
    """Write the list form, which is the one the agent reads back."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(peers, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_token(path: str):
    """The fleet secret, or ``None``. Stripped: a trailing newline is not a different one."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def write_token(path: str, token: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")


def onboard(data_dir: str, node_id: str = "", peers=(), token: str = None,
            token_file: str = None, force_token: bool = False) -> dict:
    """Create or update a node's onboarding state; return what happened.

    Refusing to change an existing secret is the point: a node that quietly adopts a
    different token is a node whose frames are refused by everyone else, and from the
    node itself that is indistinguishable from a broken mesh.
    """
    report = {"data_dir": data_dir, "node_id": node_id, "written": [], "reused": [],
              "problems": [], "peers_added": [], "peers_updated": []}
    if not os.path.isdir(data_dir):
        report["problems"].append(f"data dir does not exist: {data_dir}")
        return report

    token_path = os.path.join(data_dir, TOKEN_NAME)
    existing = read_token(token_path)
    supplied = ((token or "").strip()
                or (read_token(token_file) if token_file else None))
    if supplied and existing and supplied != existing and not force_token:
        report["problems"].append(
            f"{TOKEN_NAME} already holds a different secret: this node is already in "
            f"a fleet, and overwriting forks it (--force-token to mean it)")
        return report

    if supplied:
        write_token(token_path, supplied)
        report["written"].append(TOKEN_NAME + (" (forced)" if existing else ""))
    elif existing and not force_token:
        report["reused"].append(TOKEN_NAME)
    else:
        write_token(token_path, secrets.token_hex(TOKEN_CHARS // 2))
        report["written"].append(
            f"{TOKEN_NAME} (new {TOKEN_CHARS}-char secret; copy it to every node)")
    report["token_path"] = token_path

    if peers:
        peers_path = os.path.join(data_dir, PEERS_NAME)
        merged = {peer["id"]: peer for peer in read_peers_file(peers_path)[0]}
        for peer in peers:
            if peer["id"] in merged:
                if merged[peer["id"]] != peer:
                    report["peers_updated"].append(peer["id"])
            else:
                report["peers_added"].append(peer["id"])
            merged[peer["id"]] = peer
        write_peers_file(peers_path, [merged[key] for key in sorted(merged)])
        report["written"].append(f"{PEERS_NAME} ({len(merged)} peer(s))")
    return report


def check(data_dir: str) -> dict:
    """Report a node's onboarding state without changing anything."""
    report = {"data_dir": data_dir, "ok": True, "problems": [], "peers": [],
              "token": {"present": False, "chars": 0, "hexish": False}}
    token = read_token(os.path.join(data_dir, TOKEN_NAME))
    if token:
        report["token"] = {"present": True, "chars": len(token),
                           "hexish": all(char in "0123456789abcdef" for char in token)}
    else:
        report["problems"].append(
            f"no {TOKEN_NAME}: every mesh frame this node sends will be refused")
    peers, problems = read_peers_file(os.path.join(data_dir, PEERS_NAME))
    report["peers"] = peers
    report["problems"] = report["problems"] + problems
    if not peers:
        report["problems"].append(f"no {PEERS_NAME}: this node will dial nobody")
    report["ok"] = not report["problems"]
    return report


def render_onboard(report: dict) -> str:
    lines = [f"onboarding {report['data_dir']}"
             + (f" as {report['node_id']}" if report["node_id"] else "")]
    for item in report["written"]:
        lines.append(f"  wrote {item}")
    for item in report["reused"]:
        lines.append(f"  kept existing {item}")
    for peer_id in report["peers_added"]:
        lines.append(f"  added peer {peer_id}")
    for peer_id in report["peers_updated"]:
        lines.append(f"  updated peer {peer_id}")
    for problem in report["problems"]:
        lines.append(f"  problem: {problem}")
    if not report["problems"]:
        lines.append("  restart the node (or the app) so it re-reads these files")
    return "\n".join(lines)


def render_check(report: dict) -> str:
    token = report["token"]
    if token["present"]:
        shape = "" if token["hexish"] else ", not hex -- is this really the secret?"
        lines = [f"{report['data_dir']}: secret present ({token['chars']} chars{shape})"]
    else:
        lines = [f"{report['data_dir']}: NO secret"]
    lines.append("  peers: " + (", ".join(
        f"{peer['id']}={peer['host']}:{peer['port']}" for peer in report["peers"])
        or "none configured"))
    for problem in report["problems"]:
        lines.append(f"  problem: {problem}")
    return "\n".join(lines)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Onboard a node into the hive (shared secret + peers)")
    ap.add_argument("--data-dir", required=True, help="the node's data dir")
    ap.add_argument("--node-id", default="", help="the id this node answers to")
    ap.add_argument("--peers", default="", metavar="ID=HOST:PORT,...",
                    help=f"peers to dial; merged with any already in {PEERS_NAME}")
    ap.add_argument("--token", default=None, help="the fleet's shared secret")
    ap.add_argument("--token-file", default=None,
                    help="read the fleet secret from this file instead")
    ap.add_argument("--force-token", action="store_true",
                    help="replace an existing secret (forks the fleet unless every "
                         "node is updated)")
    ap.add_argument("--check", action="store_true",
                    help="report what is there and write nothing")
    ap.add_argument("--json", action="store_true", help="also dump the raw report")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.check:
        report = check(args.data_dir)
        print(render_check(report))
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    peers, problems = parse_peers(args.peers)
    report = onboard(args.data_dir, args.node_id, peers, args.token,
                     args.token_file, args.force_token)
    report["problems"] = report["problems"] + problems
    print(render_onboard(report))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())