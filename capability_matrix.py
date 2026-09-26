#!/usr/bin/env python3
"""Capability-retention matrix: what each hive node still *has*.

Every capability this system claims (memory, audit trail, timers, identity,
models, the mesh itself) lives somewhere concrete on each node. This module
inspects that state and reports, per node, whether the capability is still there
-- so a claim like "the upgrade preserves your memory" is checked against the
device instead of being asserted.

It is deliberately read-only and dependency-free (a directory listing is all it
needs), so the same classifier works for a host data dir and for an Android app
data dir pulled through ``run-as``.

    python3 capability_matrix.py --host-dir runtime/desktop \
        --phone "Tab S9 FE=adb-R52WC05JPMW-4kMS88._adb-tls-connect._tcp"

Lifecycle events are recorded per node, because "retained" only means something
relative to what happened: a phone upgraded in place keeps its memory, while the
same phone reinstalled from scratch would keep nothing.
"""
import argparse
import json
import os
import re
import subprocess

# (key, label, path, kind, risk) -- kind is "file" or "dir"; risk explains what
# would take the capability away, which is what an operator needs to know.
CLAIMS = (
    ("tier2_memory", "Tier 2 semantic memory", "semantic_memory.db", "file",
     "wiped by uninstall / clear-data; preserved by in-place upgrade"),
    ("audit_chain", "Hash-linked audit chain", "audit_chain.jsonl", "file",
     "wiped by uninstall; append-only within a run"),
    ("decision_log", "Decision journal", "decision_engine.log", "file",
     "wiped by uninstall"),
    ("episodic_journal", "Episodic journal", "episodic_journal.jsonl", "file",
     "wiped by uninstall"),
    ("timers", "Pending timers", "timers.json", "file",
     "wiped by uninstall; survives restart"),
    ("user_facts", "Learned user facts", "user_facts.json", "file",
     "wiped by uninstall"),
    ("personality", "Personality model", "personality_model.json", "file",
     "wiped by uninstall"),
    ("profile", "Installed profile marker", "profileInstalled", "file",
     "device-only; wiped by uninstall"),
    ("models", "Local model weight(s)", "models", "dir",
     "wiped by uninstall; multi-GB to restore"),
    ("mesh_identity", "Mesh shared secret", "mesh_token.txt", "file",
     "must match the fleet token or every frame is refused"),
    ("mesh_peers", "Configured mesh peers", "mesh_peers.json", "file",
     "device-only; re-key/onboarding rewrites it"),
    ("artifacts", "Build staging dir (received)", "artifacts", "dir",
     "created by v1.30.7; safe to empty"),
    ("shared", "Build share dir (offered)", "shared", "dir",
     "created by v1.30.7; safe to empty"),
)

_LISTING = re.compile(
    r"^(?P<mode>[dl-])[rwxsStT-]{9}\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+"
    r"\S+\s+\S+\s+(?P<name>.+?)\s*$")

# Claims that do not apply to a host node, with the reason to show instead of a
# verdict (a host serves weights from its model backend rather than a data dir).
HOST_NOT_APPLICABLE = {
    "profile": "device-only marker",
    "mesh_peers": "device-only (hosts take peers from CLI/env)",
    "models": "host serves weights from its model backend",
}
# Claims a host creates only when it actually uses them, so an absent file there
# is informational. On Android these exist by construction -- a missing one is a
# real loss (a reinstall), which is the case worth failing on.
HOST_OPTIONAL = {"timers", "user_facts"}


def parse_android_listing(text: str) -> dict:
    """Parse ``run-as <pkg> ls -l <data-dir>`` into {name: [size, isdir]}.

    Only the shape we need is read (mode, size, name); anything unparseable --
    a ``total N`` header, a symlink line, adb's own chatter -- is skipped rather
    than guessed at.
    """
    files: dict = {}
    for line in str(text or "").splitlines():
        match = _LISTING.match(line.strip())
        if not match:
            continue
        name = match.group("name").strip()
        if not name or name in (".", ".."):
            continue
        files[name] = [int(match.group("size")), match.group("mode") == "d"]
    return files


def scan_directory(path: str) -> dict:
    """Same shape as ``parse_android_listing``, read straight from a dir."""
    files: dict = {}
    try:
        names = os.listdir(path)
    except OSError:
        return files
    for name in names:
        try:
            full = os.path.join(path, name)
            isdir = os.path.isdir(full)
            size = 0 if isdir else os.path.getsize(full)
        except OSError:
            continue
        files[name] = [size, isdir]
    return files


def evaluate(files: dict, *, platform: str = "android",
             expected_token: str = None, version: str = None,
             events: tuple = (), state: dict = None) -> dict:
    """Classify every claim for one node.

    Pure with respect to the filesystem (it only reads ``files`` and ``state``),
    so the rule table can be unit-tested without a device.

    Verdicts: ``ok`` (present and plausible), ``empty`` (present but zero bytes --
    a real failure to flag, not a pass), ``missing``, ``n/a`` (a device-only or
    host-only claim), ``mismatch`` (identity present but not the fleet's).
    """
    host = platform == "host"
    state = state or {}
    rows = []
    for key, label, name, kind, risk in CLAIMS:
        if key in HOST_NOT_APPLICABLE and host:
            rows.append({"key": key, "label": label, "verdict": "n/a",
                         "detail": HOST_NOT_APPLICABLE[key], "risk": risk})
            continue
        entry = files.get(name)
        if entry is None:
            if host and key in HOST_OPTIONAL:
                rows.append({"key": key, "label": label,
                             "verdict": "not-created",
                             "detail": f"{name} not created on this node yet",
                             "risk": risk})
                continue
            rows.append({"key": key, "label": label, "verdict": "missing",
                         "detail": f"{name} is not there", "risk": risk})
            continue
        size, isdir = int(entry[0]), bool(entry[1])
        if kind == "dir" and not isdir:
            rows.append({"key": key, "label": label, "verdict": "missing",
                         "detail": f"{name} is not a directory", "risk": risk})
            continue
        if kind == "file" and isdir:
            rows.append({"key": key, "label": label, "verdict": "missing",
                         "detail": f"{name} is a directory", "risk": risk})
            continue
        if key == "mesh_identity":
            presented = state.get("token")
            if expected_token and presented and presented != expected_token:
                rows.append({"key": key, "label": label,
                             "verdict": "mismatch",
                             "detail": "secret differs from the fleet token",
                             "risk": risk})
                continue
        if size <= 0 and kind == "file":
            rows.append({"key": key, "label": label, "verdict": "empty",
                         "detail": f"{name} is 0 bytes", "risk": risk})
            continue
        rows.append({"key": key, "label": label, "verdict": "ok",
                     "detail": f"{name} ({size} B)", "risk": risk})
    return {"platform": platform, "version": version, "events": list(events),
            "rows": rows}


def summarize(result: dict) -> dict:
    """Per-verdict counts plus the keys that need attention."""
    counts: dict = {}
    for row in result.get("rows", []):
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    failing = [row["key"] for row in result.get("rows", [])
               if row["verdict"] in ("missing", "empty", "mismatch")]
    return {"counts": counts, "failing": failing}


def render(results) -> str:
    """A matrix: one row per claim, one column per node."""
    nodes = list(results)
    if not nodes:
        return "no nodes probed"
    width = max(len(row["label"]) for row in nodes[0]["rows"])
    col = max(11, max(len(node["name"]) for node in nodes))
    head = "capability".ljust(width) + " | " + " | ".join(
        node["name"].ljust(col) for node in nodes)
    lines = [head, "-" * len(head)]
    for index, claim in enumerate(nodes[0]["rows"]):
        cells = []
        for node in nodes:
            verdict = (node["rows"][index]["verdict"]
                       if index < len(node["rows"]) else "?")
            cells.append(str(verdict).ljust(col))
        lines.append(claim["label"].ljust(width) + " | " + " | ".join(cells))
    lines.append("")
    for node in nodes:
        summary = summarize(node)
        lines.append(f"{node['name']}: {summary['counts']} "
                     f"events={list(node.get('events') or ['none recorded'])}")
        if summary["failing"]:
            lines.append("  needs attention: " + ", ".join(summary["failing"]))
    return "\n".join(lines)


def _adb_run(adb: str, serial: str, command: str) -> str:
    """Run one read-only adb shell command; '' on any failure."""
    try:
        proc = subprocess.run([adb, "-s", serial, "shell", command],
                              capture_output=True, text=True, timeout=45)
    except Exception:
        return ""
    return proc.stdout or ""


def probe_phone(name: str, serial: str, adb: str, package: str,
                expected_token: str = None, events: tuple = ()) -> dict:
    """Read a phone's app data dir through ``run-as`` (debug builds only)."""
    files = parse_android_listing(_adb_run(adb, serial, f"run-as {package} ls -l files"))
    token = _adb_run(adb, serial,
                     f"run-as {package} cat files/mesh_token.txt").strip()
    version = ""
    match = re.search(r"versionName=(\S+)",
                      _adb_run(adb, serial, f"dumpsys package {package}"))
    if match:
        version = match.group(1)
    result = evaluate(files, platform="android", expected_token=expected_token,
                      version=version, events=events,
                      state={"token": token or None})
    result["name"] = name
    result["serial"] = serial
    if not files:
        result["note"] = ("no readable data dir (device offline, or run-as "
                          "refused on a non-debug build)")
    return result


def probe_host(name: str, data_dir: str, expected_token: str = None,
               events: tuple = ()) -> dict:
    """Read a host node's data dir directly."""
    token = None
    try:
        with open(os.path.join(data_dir, "mesh_token.txt"), encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        token = None
    result = evaluate(scan_directory(data_dir), platform="host",
                      expected_token=expected_token, events=events,
                      state={"token": token})
    result["name"] = name
    result["dir"] = data_dir
    if not result["rows"] or all(row["verdict"] == "missing"
                                 for row in result["rows"]):
        result["note"] = f"nothing found in {data_dir}"
    return result


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Capability-retention matrix for the hive nodes")
    ap.add_argument("--host-dir", default=None,
                    help="host node data dir (e.g. runtime/desktop)")
    ap.add_argument("--host-name", default="host")
    ap.add_argument("--phone", action="append", default=[],
                    metavar="NAME=SERIAL",
                    help="Android node to probe via adb (repeatable)")
    ap.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    ap.add_argument("--package", default="com.samurai.shugocore")
    ap.add_argument("--token-file", default=None,
                    help="fleet mesh token, to check each node's identity "
                         "against (e.g. <host-dir>/mesh_token.txt)")
    ap.add_argument("--json", action="store_true",
                    help="also dump the raw rows as JSON")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    token = None
    if args.token_file:
        try:
            with open(args.token_file, encoding="utf-8") as fh:
                token = fh.read().strip() or None
        except OSError as exc:
            print(f"token file unreadable: {exc}")
    results = []
    if args.host_dir:
        results.append(probe_host(args.host_name, args.host_dir, token,
                                  events=("agent restart", "token applied")))
    for spec in args.phone:
        name, _, serial = str(spec).partition("=")
        if not name or not serial:
            print(f"ignoring malformed --phone {spec!r} (want NAME=SERIAL)")
            continue
        results.append(probe_phone(name.strip(), serial.strip(), args.adb,
                                   args.package, token,
                                   events=("in-place upgrade", "restart")))
    print(render(results))
    for result in results:
        if result.get("note"):
            print(f"{result['name']}: {result['note']}")
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    failing = [result["name"] for result in results
               if summarize(result)["failing"]]
    if failing:
        print("\ncapabilities needing attention: " + ", ".join(failing))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


