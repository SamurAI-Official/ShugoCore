#!/usr/bin/env python3
"""Phase E: every claim, with the evidence that backs it.

The README and CHANGELOG assert things ("the hive elects one primary", "a build
reaches any node", "a follower cannot actuate"). This tool turns each claim into a
row: the check that decides it, the artifact captured from that check, and a
verdict. It never marks a claim proven because it is written down -- only because
a command exited zero or a live check saw the fact.

    python3 claim_matrix.py                  # run everything, capture artifacts
    python3 claim_matrix.py --only mesh.election --json

Artifacts land in ``runtime/evidence/<id>.txt`` so a claim can be inspected later
instead of trusted.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ARTIFACTS = os.path.join("runtime", "evidence")

# live(status_text) -> (ok, detail): pure parsers, so a captured status line can
# be re-checked in a test without a running hive.
ROLE_RE = re.compile(r"role=(\w+)")
IMPORTED_RE = re.compile(r"imported=(\d+)")


def hub_role(status_text: str) -> tuple:
    """A hub status line must show a primary lease held by this node."""
    match = ROLE_RE.search(status_text or "")
    if not match:
        return False, "no role= in the status line"
    role = match.group(1)
    if role != "primary":
        return False, f"role={role} (expected the lease holder to be primary)"
    return True, "role=primary"


def hub_imported(status_text: str) -> tuple:
    """The memory mesh must have actually moved facts, not just connected."""
    match = IMPORTED_RE.search(status_text or "")
    if not match:
        return False, "no imported= counter"
    count = int(match.group(1))
    if count <= 0:
        return False, f"imported={count} (nothing has crossed the mesh)"
    return True, f"imported={count} fact(s)"


def phone_quiet(log_text: str) -> tuple:
    """A subordinate node must not be running the model-backed loop itself."""
    hits = (log_text or "").count("ANDROID_INFERENCE generate called")
    if hits:
        return False, f"{hits} local model call(s) on a subordinate node"
    return True, "no local model calls"


def chain_present(chain_text: str) -> tuple:
    """The audit chain must exist and be hash-linked, not just a log file."""
    lines = [line for line in (chain_text or "").splitlines() if line.strip()]
    if not lines:
        return False, "the chain is empty"
    hashed = 0
    for line in lines[-50:]:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("hash"):
            hashed += 1
    if not hashed:
        return False, f"{len(lines)} line(s) but none carry a hash"
    return True, f"{len(lines)} entries, last {hashed} hashed"


LIVE_CHECKS: Dict[str, Callable[[str], tuple]] = {
    "hub_role": hub_role,
    "hub_imported": hub_imported,
    "phone_quiet": phone_quiet,
    "audit_chain": chain_present,
}

# Every claim the docs make, with the check that decides it. ``__PY__`` is
# replaced with the running interpreter, so the same matrix works on the Mac.
CLAIMS: List[Dict[str, Any]] = [
    {"id": "mesh.election",
     "claim": "the hive elects exactly one primary and survives peers restarting",
     "doc": "README 'Mesh primary election (Track 1)'",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_mesh_heartbeat",
                                             "tests.test_mesh_election"]},
                {"kind": "live", "name": "hub_role", "path": ""}]},
    {"id": "mesh.memory", "claim": "Tier 2 memory crosses the mesh",
     "doc": "README 'Memory mesh semantics'",
     "checks": [{"kind": "live", "name": "hub_imported", "path": ""}]},
    {"id": "mesh.builds",
     "claim": "a build travels node-to-node, digest-checked",
     "doc": "README 'Build transfer over the mesh'",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_mesh_artifacts"]}]},
    {"id": "governance.consent",
     "claim": "side effects need an operator grant",
     "doc": "README 'Operator consent surface'",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_consent_surface"]}]},
    {"id": "containment.actuation",
     "claim": "nothing actuates without passing every gate, and refusals never "
              "reach the wire",
     "doc": "README 'Actuation sandbox'",
     "checks": [{"kind": "command", "argv": ["__PY__", "actuation_sandbox.py",
                                             "--data-dir", "runtime/sandbox"]}]},
    {"id": "orchestration.top_down",
     "claim": "a subordinate node does not run its own model-backed loop",
     "doc": "CHANGELOG 'Top-down orchestration by measured capacity'",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_orchestration_mode"]},
                {"kind": "live", "name": "phone_quiet",
                 "path": "runtime/evidence/phone_logcat.txt"}]},
    {"id": "routing.operator_proximity",
     "claim": "the hive answers through the device closest to the operator, and "
              "stays silent otherwise",
     "doc": "response_routing.py",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_response_routing"]}]},
    {"id": "fleet.rollout",
     "claim": "rollouts are allowlisted, digest-checked and refuse by default",
     "doc": "README 'Fleet deployment action types'",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_fleet_deploy"]}]},
    {"id": "audit.chain",
     "claim": "every decision and execution is recorded in a hash-linked chain",
     "doc": "audit.py",
     "checks": [{"kind": "live", "name": "audit_chain",
                 "path": "runtime/desktop/audit_chain.jsonl"}]},
]


def _run_command(argv, cwd, timeout=300):
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def run_claim(claim: Dict[str, Any], *, repo: str = REPO_ROOT,
              artifacts: str = DEFAULT_ARTIFACTS, python: str = None,
              status_file: Optional[str] = None) -> Dict[str, Any]:
    """Run one claim's checks, capture the evidence, and judge it.

    Verdicts: ``proven`` (every check passed), ``failed`` (a check did not), and
    ``unproven`` when a live check could not be evaluated -- which is not the same
    as failing, and is never reported as provenance.
    """
    python = python or sys.executable
    lines: List[str] = []
    verdict = "proven"
    for check in claim.get("checks", []):
        if check.get("kind") == "command":
            argv = [python if part == "__PY__" else part
                    for part in check["argv"]]
            code, out = _run_command(argv, repo)
            lines.append(f"$ {' '.join(argv)}\n[exit {code}]\n{out[-4000:]}")
            if code != 0:
                verdict = "failed"
        elif check.get("kind") == "live":
            func = LIVE_CHECKS.get(str(check.get("name")))
            path = check.get("path") or status_file
            if func is None or not path:
                lines.append(f"live {check.get('name')}: not run (pass "
                             f"--status-file to evaluate it)")
                if verdict == "proven":
                    verdict = "unproven"
                continue
            try:
                with open(os.path.join(repo, path), encoding="utf-8",
                          errors="replace") as handle:
                    text = handle.read()
            except OSError as exc:
                lines.append(f"live {check.get('name')}: unreadable ({exc})")
                if verdict == "proven":
                    verdict = "unproven"
                continue
            passed, detail = func(text)
            lines.append(f"live {check.get('name')} [{path}]: "
                         f"{'ok' if passed else 'FAILED'} - {detail}")
            if not passed:
                verdict = "failed"
    target = os.path.join(repo, artifacts, claim["id"] + ".txt")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(f"claim: {claim['claim']}\nwhere: {claim.get('doc', '')}\n"
                     f"verdict: {verdict}\n\n" + "\n\n".join(lines) + "\n")
    return {"id": claim["id"], "claim": claim["claim"],
            "doc": claim.get("doc", ""), "status": verdict,
            "artifact": os.path.relpath(target, repo)}


def render(rows) -> str:
    idw = max((len(row["id"]) for row in rows), default=6)
    lines = [f"{'claim'.ljust(idw)} | {'verdict':<9} | evidence",
             "-" * (idw + 30)]
    counts: Dict[str, int] = {}
    for row in rows:
        lines.append(f"{row['id'].ljust(idw)} | {row['status']:<9} | "
                     f"{row['artifact']}")
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    lines.append("")
    lines.append(" / ".join(f"{count} {status}" for status, count
                            in sorted(counts.items())) or "nothing to report")
    for row in rows:
        if row["status"] != "proven":
            lines.append(f"  {row['status'].upper()} {row['id']}: "
                         f"{row['claim']}")
    return "\n".join(lines)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase E: every claim, with the evidence that backs it")
    ap.add_argument("--only", action="append", default=[], metavar="ID")
    ap.add_argument("--artifacts", default=DEFAULT_ARTIFACTS)
    ap.add_argument("--repo", default=REPO_ROOT)
    ap.add_argument("--status-file", default=None,
                    help="captured host status line(s) for the live claims "
                         "(role, imported)")
    ap.add_argument("--json", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    rows = [run_claim(claim, repo=args.repo, artifacts=args.artifacts,
                      status_file=args.status_file)
            for claim in CLAIMS
            if not args.only or claim["id"] in set(args.only)]
    print(render(rows))
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    return 1 if any(row["status"] == "failed" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())

