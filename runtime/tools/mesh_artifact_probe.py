"""Live proof: move a build between two nodes over the mesh.

    python runtime/tools/mesh_artifact_probe.py --id shugo-artifactlab \
        --port 9021 --peer shugo-desktop=127.0.0.1:9000 \
        --fetch app-debug.apk --offer build-note.txt

The probe is a plain mesh node -- no engine, no memory, no adb -- which is
exactly the point: a build travels node-to-node over the ShugoNet transport,
digest-verified on both ends, with no cable in the loop.

It prints one JSON summary at the end so a caller (or a test) can assert on the
digests rather than trust a log line.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_runtime import ShugonetAgentRuntime  # noqa: E402


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Mesh artifact transfer probe")
    ap.add_argument("--id", default="shugo-artifactlab")
    ap.add_argument("--port", type=int, default=9021)
    ap.add_argument("--peer", action="append", default=[],
                    metavar="ID=HOST:PORT")
    ap.add_argument("--token", default=os.environ.get("SHUGOCORE_MESH_TOKEN"))
    ap.add_argument("--share", default="runtime/artifactlab/shared",
                    help="directory this probe offers over the mesh")
    ap.add_argument("--stage", default="runtime/artifactlab/stage",
                    help="directory anything it fetches lands in")
    ap.add_argument("--fetch", default=None, metavar="NAME",
                    help="artifact to pull from the peer")
    ap.add_argument("--fetch-peer", default="shugo-desktop")
    ap.add_argument("--offer", default=None, metavar="NAME",
                    help="file to create in the share dir and offer to a peer "
                         "(the peer pulls it)")
    ap.add_argument("--offer-peer", default="shugo-desktop")
    ap.add_argument("--wait", type=float, default=20.0,
                    help="seconds to stay up after the jobs run, so the peer "
                         "can finish pulling an offer")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    share = str(Path(args.share))
    stage = str(Path(args.stage))
    os.makedirs(share, exist_ok=True)
    os.makedirs(stage, exist_ok=True)

    if args.offer:
        note = Path(share) / args.offer
        note.write_text(
            f"build note from {args.id} at "
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\n", encoding="utf-8")

    peers = {}
    for spec in args.peer:
        pid, _, hostport = spec.partition("=")
        host, _, port = hostport.partition(":")
        if pid and host:
            peers[pid.strip()] = (host.strip(), int(port or 9000))

    runtime = ShugonetAgentRuntime(
        agent_id=args.id, host="127.0.0.1", port=args.port,
        auth_token=args.token, artifact_root=share, artifact_dir=stage,
        heartbeat_interval=0)
    for pid, (host, port) in peers.items():
        runtime.add_peer(pid, host, port)
    runtime.start()
    runtime.reconnect_peers()

    summary = {"id": args.id, "port": args.port, "peers": sorted(peers),
               "fetch": None, "offer": None, "shared": []}
    try:
        deadline = time.time() + 10.0
        while time.time() < deadline and runtime.reconnect_peers() < len(peers):
            time.sleep(0.25)
        summary["connected"] = runtime.status()["connected_peers"]
        summary["shared"] = [item["name"] for item in runtime.list_artifacts()]

        if args.fetch:
            result = runtime.fetch_artifact(args.fetch_peer, args.fetch)
            if result.get("status") == "success":
                result = dict(result)
                result["verified"] = (
                    sha256_file(result["path"]) == result["sha256"])
            summary["fetch"] = result
        if args.offer:
            summary["offer"] = runtime.offer_artifact(args.offer_peer,
                                                     args.offer)
        if args.wait > 0:
            time.sleep(args.wait)
        summary["receipts"] = runtime.artifact_receipts()
    finally:
        runtime.stop()
    print("PROBE_SUMMARY " + json.dumps(summary, sort_keys=True))
    ok = True
    if args.fetch:
        ok = ok and bool(summary["fetch"]
                         and summary["fetch"].get("status") == "success"
                         and summary["fetch"].get("verified"))
    if args.offer:
        ok = ok and bool(summary["offer"]
                         and summary["offer"].get("status") == "success")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
