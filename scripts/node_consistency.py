#!/usr/bin/env python3
"""Check a node against the fleet's declared state (run it on any node).

Consistency in this fleet has four parts, and every incident so far came from one
of them drifting silently:

1. **the repo** -- which commit this node is on, and whether it matches origin;
2. **the pin** -- the llama.cpp submodule checkout must equal the commit the
   parent repo records: a host and a peripheral built from different trees can
   disagree about the wire protocol while every local test passes;
3. **the bundle** -- the Python modules inside the Android app must match the
   repo's copies, or a device runs different code than the repo describes;
4. **the toolchain** -- the layer-split host binaries exist *and* can offload
   (`--rpc` present); a build that compiles and cannot offload is how that went
   wrong once.

    python scripts/node_consistency.py
    python scripts/node_consistency.py --data-dir runtime/desktop
    python scripts/node_consistency.py --expect-commit 7eb9b54

Exit code 0 means consistent; 1 means at least one difference. Nothing here
writes: it reports, so it is safe on every node including the primary.
"""
import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_llama_rpc as blr  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_REL = Path("platforms/android/app/src/main/python")
SUBMODULE_REL = Path("platforms/android/app/src/main/cpp/llama.cpp")
DEFAULT_HOST_BIN = Path("G:/Android/llama-rpc/host/bin")


def file_sha(path, read_bytes=None) -> str:
    """SHA-256 of a file, or "" when it cannot be read."""
    reader = read_bytes
    try:
        if reader is None:
            with open(path, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        return hashlib.sha256(reader(str(path))).hexdigest()
    except Exception:
        return ""


def bundle_differences(root, bundle_rel=BUNDLE_REL, sha=None) -> list:
    """Python files in the Android bundle that do not match the repo's copies.

    Returns ``[(relative_path, reason)]``; empty means the bundle is a faithful
    mirror. A module that exists only in the bundle is reported too: it means the
    app ships code the repo does not have.
    """
    sha = sha or file_sha
    root = Path(root)
    bundle = root / bundle_rel
    problems = []
    if not bundle.exists():
        return [(str(bundle_rel), "bundle directory missing")]
    for path in sorted(bundle.rglob("*.py")):
        rel = path.relative_to(bundle)
        mirror = root / rel
        if not mirror.exists():
            problems.append((rel.as_posix(), "in bundle, not in repo"))
            continue
        if sha(path) != sha(mirror):
            problems.append((rel.as_posix(), "bundle differs from repo"))
    return problems


def repo_state(repo_root=REPO_ROOT, runner=None) -> dict:
    """Branch, HEAD and how far this checkout is from its upstream."""
    runner = runner or subprocess.run

    def _git(*args) -> str:
        out = runner(["git", *args], cwd=str(repo_root), capture_output=True,
                     text=True)
        return (getattr(out, "stdout", "") or "").strip()

    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    head = _git("rev-parse", "HEAD")
    counts = _git("rev-list", "--left-right", "--count", f"origin/{branch}...HEAD")
    ahead = behind = ""
    if "\t" in counts:
        # left = commits only upstream has (we are behind), right = only ours.
        behind, _, ahead = counts.partition("\t")
    return {"branch": branch, "head": head, "ahead": ahead.strip(),
            "behind": behind.strip()}


def submodule_state(repo_root=REPO_ROOT, submodule_rel=SUBMODULE_REL,
                    runner=None) -> dict:
    """Pin vs checkout for the llama.cpp submodule (a drift here is silent)."""
    checkout = blr.submodule_commit(repo_root=repo_root, submodule=submodule_rel,
                                    runner=runner)
    pin = blr.submodule_pin(repo_root=repo_root, submodule=submodule_rel,
                            runner=runner)
    return {"pin": pin, "checkout": checkout,
            "agree": bool(pin and checkout and pin == checkout)}


def host_tool_state(bin_dir=None, run_capture=None) -> dict:
    """The layer-split host binaries, and whether they can actually offload."""
    resolved = Path(bin_dir or os.environ.get("SHUGOCORE_LLAMA_HOST",
                                              DEFAULT_HOST_BIN))
    server = blr.find_tool(("llama-server.exe", "llama-server"), None,
                           [str(resolved)])
    rpc = blr.find_tool(("ggml-rpc-server.exe", "ggml-rpc-server"), None,
                        [str(resolved)])
    return {"bin_dir": str(resolved), "llama_server": server, "rpc_server": rpc,
            # A server without --rpc cannot take part in a layer split, however
            # cleanly it compiled -- so "exists" is not the question.
            "can_offload": bool(server) and blr.verify_rpc_flag(server,
                                                               run_capture)}


def identity_state(data_dir=None) -> dict:
    """This node's persisted id, and whether it can measure its own memory.

    A node that advertises no free memory is ineligible in the election and is
    skipped by the layer planner: it looks alive and contributes nothing.
    """
    node_id = ""
    if data_dir:
        try:
            node_id = (Path(data_dir) / "node_id.txt").read_text(
                encoding="utf-8").strip()
        except OSError:
            node_id = ""
    try:
        from mesh_election import available_memory_bytes
        mem = int(available_memory_bytes() or 0)
    except Exception:
        mem = 0
    return {"node_id": node_id, "mem_available_bytes": mem,
            "advertises_headroom": mem > 0}


def render(repo, submodule, bundle, tools, identity, expect_commit="") -> tuple:
    """(lines, consistent) -- the whole verdict, so the logic stays testable."""
    lines, ok = [], True

    def row(label, value, good, note=""):
        nonlocal ok
        ok = ok and bool(good)
        lines.append(f"{'PASS' if good else 'DIFF'}  {label}: {value}"
                     + (f"  ({note})" if note else ""))

    head = (repo or {}).get("head", "")
    row("repo commit", (head or "unknown")[:12], bool(head),
        f"branch {(repo or {}).get('branch', '?')}")
    if expect_commit:
        row("expected commit", expect_commit[:12], head.startswith(expect_commit),
            "from --expect-commit")
    ahead, behind = (repo or {}).get("ahead", ""), (repo or {}).get("behind", "")
    if ahead or behind:
        row("upstream", f"ahead {ahead}, behind {behind}",
            ahead in ("", "0") and behind in ("", "0"),
            "push or pull so nodes agree")
    pinned = (submodule or {}).get("agree")
    row("llama.cpp pin", (submodule or {}).get("checkout", "")[:12] or "unknown",
        pinned, "" if pinned else
        "checkout != pin: build both ends from the pinned commit")
    row("android bundle", f"{len(bundle)} difference(s)", not bundle,
        "; ".join(f"{name}: {why}" for name, why in (bundle or [])[:3]))
    server = (tools or {}).get("llama_server")
    if server:
        row("host llama-server", server, (tools or {}).get("can_offload"),
            "" if (tools or {}).get("can_offload") else
            "no --rpc in the built server: it cannot offload")
        row("host rpc-server", (tools or {}).get("rpc_server") or "missing",
            bool((tools or {}).get("rpc_server")),
            "peripheral binary for this host")
    else:
        row("host llama-server", "missing", False,
            "python scripts/build_llama_rpc.py --host  (looked in "
            f"{(tools or {}).get('bin_dir', '?')})")
    row("node identity", (identity or {}).get("node_id") or "not set",
        bool((identity or {}).get("node_id")),
        "the id peers dial must match the id this node advertises")
    mem = (identity or {}).get("mem_available_bytes", 0)
    row("advertised headroom", f"{mem} B",
        bool((identity or {}).get("advertises_headroom")),
        "" if (identity or {}).get("advertises_headroom") else
        "no measurable memory: ineligible in the election and skipped by the "
        "layer planner")
    return lines, ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--data-dir", default="",
                    help="node data dir holding node_id.txt")
    ap.add_argument("--bin-dir", default="", help="layer-split host binaries")
    ap.add_argument("--expect-commit", default="",
                    help="report a difference unless HEAD is this commit")
    args = ap.parse_args(argv)
    repo_root = Path(args.repo)
    lines, consistent = render(
        repo_state(repo_root),
        submodule_state(repo_root),
        bundle_differences(repo_root),
        host_tool_state(args.bin_dir or None),
        identity_state(args.data_dir or None),
        args.expect_commit)
    print(f"node consistency ({os.name}, {repo_root})")
    print("\n".join(lines))
    print("consistent" if consistent
          else "NOT consistent -- see the DIFF lines above")
    return 0 if consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())

