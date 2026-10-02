#!/usr/bin/env python3
"""Prove the headset path: a Quest 3 reaches the agent over the LAN and its request is executed.

The desktop scaffold session (`xr_session.py`) proves the *surface*; this proves the *device*.
Everything here was found by asking, not by assuming: the headset is located over adb, the
desktop's address is derived from the headset's own subnet, and the token gate is exercised
rather than described.

    python runtime/tools/quest_probe.py

It writes ``runtime/evidence/quest3.reach.txt`` in the shape ``claim_matrix`` judges:

    [HEAD  ] device=Quest 3 serial=... android=14 ip=192.168.1.151
    [HOST  ] agent=http://192.168.1.152:11435 token=set
    [STATUS] GET /api/v1/status -> 200 ...
    [TASK  ] POST /api/v1/task -> 200 ...
    [ANSWER] the agent decided: {'action_type': 'speak', ...}   <- from the server's own log
    [AUTH  ] GET /api/v1/status without the token -> 401 ...

Two facts make this worth doing over the wire instead of over adb's convenience: the request
leaves the headset and arrives with the headset's own source address, and the answer is read
from the *server's* log, not from the headset's word for it.

With no headset attached nothing is invented: the transcript says so and the row stays
``unproven`` -- which is not the same as failed.
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = os.path.join("runtime", "evidence", "quest3.reach.txt")
DEFAULT_DATA = os.path.join("runtime", "quest_probe")
TOKEN = "quest-probe-token"
QUEST_MARKERS = ("product:eureka", "model:Quest_3", "model:Quest_2", "Quest")
ADB_HINTS = (
    r"G:\Android\Sdk\platform-tools\adb.exe",
    r"C:\Android\Sdk\platform-tools\adb.exe",
    os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
)
REMOTE_DIR = "/data/local/tmp"
BODY = b'{"type": "conversation", "content": "hello from the Quest 3"}'


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--adb", default=os.environ.get("SHUGOCORE_ADB", ""))
    ap.add_argument("--serial", default="", help="the headset's adb serial (default: found)")
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--timeout", type=float, default=120.0)
    return ap.parse_args(argv)


def find_adb(given: str) -> str:
    """The adb binary, or "" -- a stated search order, never a guess."""
    if given:
        return given if os.path.isfile(given) or shutil.which(given) else ""
    found = shutil.which("adb")
    if found:
        return found
    for hint in ADB_HINTS:
        if os.path.isfile(hint):
            return hint
    return ""


def adb_run(adb: str, args, timeout: float = 30.0):
    """Run ``adb <args>``; return ``(returncode, output)``. Vector args, no shell here."""
    try:
        proc = subprocess.run(  # noqa: S603 - vector args, no shell
            [adb, *args], capture_output=True, text=True, timeout=timeout, shell=False)
    except Exception as exc:
        return 126, f"{type(exc).__name__}: {exc}"
    return int(proc.returncode or 0), (proc.stdout or "") + (proc.stderr or "")


def find_quest(adb: str, wanted: str = ""):
    """``(serial, name)`` for an attached headset, or ``("", "")``.

    Matched on the device's own reported product/model rather than on a serial written down
    once: a headset that is re-paired keeps working, and a phone that merely sits on the desk
    is not mistaken for one.
    """
    rc, out = adb_run(adb, ["devices", "-l"])
    if rc != 0:
        return "", ""
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[1] != "device":
            continue
        serial = fields[0]
        if wanted and serial != wanted:
            continue
        if not any(marker in line for marker in QUEST_MARKERS):
            continue
        model = next((f.split(":", 1)[1] for f in fields if f.startswith("model:")), "Quest")
        return serial, model.replace("_", " ")
    return "", ""


def device_ip(adb: str, serial: str) -> str:
    """The headset's own wlan address, so the desktop can be found in the same subnet."""
    rc, out = adb_run(adb, ["-s", serial, "shell", "ip -4 addr show wlan0"])
    if rc != 0:
        return ""
    found = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    return found.group(1) if found else ""


def desktop_ip_for(device_addr: str) -> str:
    """This machine's address in the headset's subnet (empty when there is none).

    Derived from the headset rather than listed: the desk has more than one interface (a
    virtual adapter answers 192.168.56.1 here), and only one of them is on the fleet's LAN.
    """
    prefix = ".".join(str(device_addr).split(".")[:3])
    if not prefix:
        return ""
    try:
        host = socket.gethostname()
        candidates = {info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)}
    except Exception:
        candidates = set()
    try:
        candidates.add(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    for address in sorted(candidates):
        if address.startswith(prefix + "."):
            return address
    return ""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(host: str, port: int, token: str, budget: float = 90.0) -> bool:
    """Poll /api/v1/status until the server answers, or give up. Readiness is observed."""
    url = f"http://{host}:{port}/api/v1/status"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=3) as reply:
                if 200 <= reply.status < 300:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_server(port: int, data_dir: Path, token: str, log_file=None):
    """Start a desktop server bound to the LAN, so the headset can reach it.

    Bound to ``0.0.0.0`` on purpose: a loopback bind is invisible to a headset, and the whole
    question here is whether the *headset* can reach the agent. The token is required for
    exactly that reason -- a LAN bind without one is refused unless the operator opts out, and
    this probe does not opt out.

    Output goes straight to ``log_file`` rather than a pipe: nobody drains a pipe here, and a
    server that logs a request per call would fill one and stall mid-probe.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "shugocore_server.py", "--host", "0.0.0.0", "--port",
            str(port), "--backend", "stub", "--model", "shugocore-local", "--no-mobile",
            "--memory-db-path", str(data_dir / "semantic_memory.db"),
            "--audit-path", str(data_dir / "audit_chain.jsonl")]
    env = dict(os.environ)
    env["SHUGOCORE_SERVER_TOKEN"] = token
    env["PYTHONUNBUFFERED"] = "1"
    # The log is read back as UTF-8: without this the child writes in the console codepage and
    # an agent that answers with an em-dash produces a byte UTF-8 cannot decode -- the evidence
    # then shows a replacement character instead of the words the agent actually chose.
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        return subprocess.Popen(  # noqa: S603 - our own tool, vector args, no shell
            argv, cwd=str(ROOT),
            stdout=(log_file if log_file is not None else subprocess.DEVNULL),
            stderr=subprocess.STDOUT, text=True, env=env, shell=False)
    except Exception:
        return None


def stop_process(proc) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def write_requests(host: str, port: int, token: str, out_dir: Path) -> dict:
    """The exact bytes the headset will send, so quoting can never mangle a request.

    An Authorization header, and a body with a known Content-Length: the same two things the
    scaffold's bridge builds with the same header and the same JSON body.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "req_status.txt": (
            b"GET /api/v1/status HTTP/1.0\r\n"
            b"Authorization: Bearer " + token.encode() + b"\r\n\r\n"),
        "req_task.txt": (
            b"POST /api/v1/task HTTP/1.0\r\n"
            b"Authorization: Bearer " + token.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(BODY)).encode() + b"\r\n\r\n" + BODY),
        "req_notoken.txt": b"GET /api/v1/status HTTP/1.0\r\n\r\n",
    }
    written = {}
    for name, payload in files.items():
        path = out_dir / name
        path.write_bytes(payload)
        written[name] = str(path)
    return written


def push(adb: str, serial: str, local: str) -> tuple:
    """Copy a request onto the headset; returns ``(ok, remote_path)``."""
    remote = f"{REMOTE_DIR}/{Path(local).name}"
    rc, out = adb_run(adb, ["-s", serial, "push", str(local), remote], timeout=60.0)
    return rc == 0 and "1 file pushed" in out, remote


def ask_headset(adb: str, serial: str, remote: str, host: str, port: int,
                seconds: float = 20.0) -> str:
    """Have the headset send the request and print what came back.

    ``nc`` reads the request from a file and writes the response to stdout, so the bytes come
    from the headset -- this is the headset's request, not the desktop's on its behalf. The
    redirection is the device shell's, which is why the command is one string: adb has no
    notion of our pipes and must not be given one.
    """
    command = f"nc -w {int(seconds)} {host} {port} < {remote}"
    rc, out = adb_run(adb, ["-s", serial, "shell", command], timeout=seconds + 30.0)
    return out


def http_status(response: str) -> int:
    """The status code in a raw response, or 0 when there was none."""
    found = re.search(r"^HTTP/\d\.\d\s+(\d{3})", response or "", re.MULTILINE)
    return int(found.group(1)) if found else 0


def http_body(response: str) -> str:
    """The response body, after the header block, collapsed to one line.

    Collapsed because the transcript is read by a line-based parser: a body left with its own
    newlines would look like several transcript lines and none of them would be the status.
    """
    text = (response or "").replace("\r\n", "\n")
    for separator in ("\n\n", "\n \n"):
        if separator in text:
            text = text.split(separator, 1)[1]
            break
    else:
        # No blank line captured: drop everything up to the last header-looking line.
        lines = text.splitlines()
        keep = []
        for line in lines:
            if keep or not re.match(r"^(HTTP/|Server:|Date:|Content-|Connection:|Transfer-)",
                                    line):
                keep.append(line)
        text = "\n".join(keep)
    return " ".join(text.split())


def read_decision(log_text: str) -> dict:
    """The agent's own decision, taken from the *server's* log rather than the headset's word.

    This is why the server side is read at all: a headset learning 'not_implemented' says
    nothing about what the agent decided to do, and on a node with no loudspeaker the decision
    is where the answer lives.

    The logger prints a Python ``dict`` repr (single quotes), not JSON, so ``literal_eval`` is
    what parses it; ``json`` is kept as the fallback for a logger configured differently.
    """
    import ast

    decision = {}
    for line in (log_text or "").splitlines():
        if "Decision:" not in line:
            continue
        start = line.find("{")
        if start < 0:
            continue
        blob = line[start:].strip()
        parsed = None
        for parse in (ast.literal_eval, json.loads):
            try:
                parsed = parse(blob)
                break
            except Exception:
                continue
        if isinstance(parsed, dict) and parsed.get("action_type"):
            decision = parsed
    return decision


def decision_text(decision: dict) -> str:
    """The words inside a decision, wherever the model put them."""
    params = decision.get("params") if isinstance(decision, dict) else None
    if isinstance(params, dict):
        for key in ("text", "utterance"):
            if str(params.get(key) or "").strip():
                return str(params[key]).strip()
    return ""


def transcript_lines(facts: dict) -> list:
    """The proof as lines. Every value was observed on the wire or read from a log."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    lines = [f"quest probe: {stamp}  desktop={facts.get('desktop') or 'unknown'}"]
    if facts.get("error"):
        lines.append(f"error={facts['error']}")
    if not facts.get("serial"):
        lines.append("[HEAD  ] no Quest headset is attached over adb")
    else:
        lines.append(f"[HEAD  ] device={facts.get('model')} serial={facts['serial']} "
                     f"android={facts.get('android')} abi={facts.get('abi')} "
                     f"ip={facts.get('device_ip')}")
        lines.append(f"[HOST  ] agent=http://{facts.get('desktop')}:{facts.get('port')} "
                     f"token={'set' if facts.get('token') else 'none'} "
                     f"(bound 0.0.0.0 so the headset can reach it)")
    if facts.get("status"):
        lines.append(f"[STATUS] GET /api/v1/status -> {facts['status']} "
                     f"{str(facts.get('status_body') or '')[:160]}")
    if facts.get("task"):
        lines.append(f"[TASK  ] POST /api/v1/task -> {facts['task']} "
                     f"{str(facts.get('task_body') or '')[:160]}")
    decision = facts.get("decision") or {}
    if decision:
        lines.append(
            "[ANSWER] the agent decided: "
            + json.dumps({"action_type": decision.get("action_type"),
                          "params": decision.get("params")}, sort_keys=True)[:200]
            + "   <- from the server's own log")
        text = decision_text(decision)
        if text:
            lines.append(f"[ANSWER] what the agent said: {text[:200]}")
    if facts.get("notoken"):
        lines.append(f"[AUTH  ] GET /api/v1/status with no token -> {facts['notoken']} "
                     f"{str(facts.get('notoken_body') or '')[:80]}")
    if facts.get("server_tail"):
        lines.append("server said (last lines):")
        lines.extend(f"    {str(line).strip()[:160]}" for line in facts["server_tail"])
    lines.append(
        "verdict: reached={reached} task={task} answer={answer} auth={auth}".format(
            reached=bool(facts.get("status") == 200 and facts.get("device_ip")),
            task=facts.get("task", 0), answer=bool(decision),
            auth=facts.get("notoken", 0)))
    return lines


def read_log_text(path: Path) -> str:
    """The server's log as text, decoded tolerantly.

    UTF-8 first, then the platform's own codepage: the child is told to write UTF-8, but a
    log written before that (or by a differently configured server) must still be readable
    rather than replaced character by character.
    """
    try:
        raw = path.read_bytes()
    except Exception:
        return ""
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def write_and_report(args, facts: dict, code: int) -> int:
    lines = transcript_lines(facts)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The server's own log, kept beside the transcript: the [ANSWER] line is read from it, so
    # keeping it means that quote can be checked against its source instead of trusted.
    log_path = Path(os.path.dirname(args.out) or ".") / "quest3.server.log"
    log_path.write_text(facts.get("server_log") or "", encoding="utf-8")
    for line in lines:
        print(line, flush=True)
    return code


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir)
    facts = {"serial": "", "model": "", "device_ip": "", "desktop": "", "port": 0,
             "token": True, "error": "", "decision": {}, "server_log": "",
             "server_tail": [], "status": 0, "task": 0, "notoken": 0}
    code = 1
    adb = find_adb(args.adb)
    if not adb:
        facts["error"] = "no adb: pass --adb or put one on PATH"
        # Not a failure of the work: the instrument is missing, so the claim could not be
        # evaluated. The transcript says so and the row reads unproven.
        return write_and_report(args, facts, 0)
    serial, model = find_quest(adb, args.serial)
    if not serial:
        facts["error"] = ("no Quest headset is attached over adb, so nothing was probed "
                          "(unproven, not failed)")
        return write_and_report(args, facts, 0)
    facts.update({"serial": serial, "model": model})
    rc, out = adb_run(adb, ["-s", serial, "shell", "getprop ro.build.version.release"])
    facts["android"] = out.strip().splitlines()[0].strip() if out.strip() else ""
    rc, out = adb_run(adb, ["-s", serial, "shell", "getprop ro.product.cpu.abi"])
    facts["abi"] = out.strip().splitlines()[0].strip() if out.strip() else ""
    facts["device_ip"] = device_ip(adb, serial)
    if not facts["device_ip"]:
        facts["error"] = "the headset reports no wlan address, so it is not on the LAN"
        return write_and_report(args, facts, 1)
    facts["desktop"] = desktop_ip_for(facts["device_ip"])
    if not facts["desktop"]:
        facts["error"] = (f"this desktop has no address on the headset's subnet "
                          f"({facts['device_ip']}); nothing was started")
        return write_and_report(args, facts, 1)
    port = free_port()
    facts["port"] = port
    log_path = data_dir / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = None
    server = None
    try:
        log_file = open(str(log_path), "w", encoding="utf-8")
        server = start_server(port, data_dir, TOKEN, log_file)
        if server is None:
            facts["error"] = "the desktop server did not start"
        elif not wait_for_server("127.0.0.1", port, TOKEN):
            facts["error"] = "the desktop server never answered on the loopback"
        else:
            requests = write_requests(facts["desktop"], port, TOKEN, data_dir)
            pushed = {}
            for name, local in requests.items():
                ok, remote = push(adb, serial, local)
                pushed[name] = remote if ok else ""
            if not pushed.get("req_task.txt"):
                facts["error"] = "could not put the task request on the headset"
            else:
                status = ask_headset(adb, serial, pushed["req_status.txt"],
                                     facts["desktop"], port)
                facts["status"] = http_status(status)
                facts["status_body"] = http_body(status)
                task = ask_headset(adb, serial, pushed["req_task.txt"], facts["desktop"],
                                   port, 30.0)
                facts["task"] = http_status(task)
                facts["task_body"] = http_body(task)
                auth = ask_headset(adb, serial, pushed["req_notoken.txt"],
                                   facts["desktop"], port)
                facts["notoken"] = http_status(auth)
                facts["notoken_body"] = http_body(auth)
                code = 0 if facts["status"] == 200 else 1
    finally:
        stop_process(server)
        time.sleep(0.5)
        if log_file is not None:
            try:
                log_file.flush()
                log_file.close()
            except Exception:
                pass
        try:
            text = read_log_text(log_path)
        except Exception:
            text = ""
        facts["server_log"] = text
        facts["decision"] = read_decision(text)
        kept = [line for line in text.splitlines() if line.strip()]
        facts["server_tail"] = kept[-6:]
    if facts["error"]:
        code = 1
    return write_and_report(args, facts, code)


if __name__ == "__main__":
    raise SystemExit(main())