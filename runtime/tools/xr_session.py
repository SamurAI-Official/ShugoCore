#!/usr/bin/env python3
"""Prove the XR world: an operator in the virtual space reaches the agent, and is answered.

Two real processes, the same ones the fleet uses:

  1. a ShugoCore desktop server -- the Ollama wire contract plus the engine API that the
     scaffold's bridge speaks;
  2. the actual Godot scaffold, headless, in whatever presence mode it can honestly reach:
     ``desktop_preview`` on a machine with no headset, which is a real observed state and not
     a simulation of one.

The scaffold's own autoloads do the talking (``ShugoCoreBridge`` for the wire, ``XRBootstrap``
for presence), so the transcript is the scaffold's behaviour rather than a parallel client's.
It writes ``runtime/evidence/world.xr.txt`` in the shape ``claim_matrix`` judges:

    [WORLD  ] xr
    [PRESENCE] mode=desktop_preview
    [GOAL   ] operator (in the virtual space): ...
    [ACTION ] execute_task conversation (gated) -> status=... stages=...
    [REPLY  ] ...

Nothing is invented. No headset means no headset; a scaffold that cannot reach the agent, or
an agent that never answers, produces a transcript that says so and exits non-zero.

    python runtime/tools/xr_session.py

Needs a Godot binary (``--godot``, ``$SHUGOCORE_GODOT``, or one on PATH / in ``G:\\godot``).
Godot imports the project on first use; that step is done here and reported, because an
unimported project cannot resolve its own ``class_name`` globals and refuses to start.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROJECT = "platforms/godot"
DEFAULT_OUT = os.path.join("runtime", "evidence", "world.xr.txt")
## The operator session logs to its own file. It is not a transcript of the claim -- it has
## no end and no single exchange to judge -- and it must never overwrite the scripted one
## that the claims matrix reads. Absolute for the same reason `session_log_path` is: Godot
## resolves the path itself, and a relative one with backslashes fails to open.
INTERACTIVE_LOG = str(Path(os.path.dirname(DEFAULT_OUT) or ".").resolve()
                      / "world.xr.interactive.log")
DEFAULT_DATA = os.path.join("runtime", "xr_node")
WORLD_KEYS = ("[WORLD", "[PRESENCE]", "[GOAL", "[ACTION", "[REPLY", "[SESSION")
GODOT_HINTS = ("godot", "Godot_v4", "godot4")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--godot", default=os.environ.get("SHUGOCORE_GODOT", ""),
                    help="the Godot binary; defaults to $SHUGOCORE_GODOT, then PATH, "
                         "then G:\\godot")
    ap.add_argument("--project", default=PROJECT)
    ap.add_argument("--data-dir", default=DEFAULT_DATA,
                    help="where the server keeps its memory DB and audit chain")
    ap.add_argument("--port", type=int, default=0,
                    help="port for the server (0 picks a free one)")
    ap.add_argument("--backend", default="stub",
                    help="the server's model backend; stub needs no model to be running")
    ap.add_argument("--backend-url", default="",
                    help="the backend's base URL (e.g. the machine's LM Studio at "
                         "http://127.0.0.1:1234); empty uses the backend's own default")
    ap.add_argument("--model", default="shugocore-local")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--interactive", action="store_true",
                    help="run the scaffold in front of an operator instead of scripting it: "
                         "a real XR session (no --headless), no frame budget, no auto-quit. "
                         "Ctrl+C stops it")
    return ap.parse_args(argv)


def find_godot(given: str) -> str:
    """The Godot binary, or "" -- searched in a stated order, never guessed at."""
    if given:
        return given if os.path.isfile(given) or shutil.which(given) else ""
    for name in ("godot", "godot4", "Godot", "Godot_v4.7.2-stable_win64_console"):
        found = shutil.which(name)
        if found:
            return found
    roots = [Path("G:/godot"), Path("C:/Program Files/Godot"), Path("C:/godot")]
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ("Godot*console*.exe", "Godot*.exe", "godot*"):
            for candidate in sorted(root.glob(pattern)):
                if candidate.is_file():
                    return str(candidate)
    return ""


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def project_imported(project: Path) -> bool:
    """True when Godot has scanned the project, so its ``class_name`` globals resolve."""
    return (project / ".godot" / "global_script_class_cache.cfg").is_file()


def run_godot(godot: str, project: str, extra, url: str, timeout: float,
              headless: bool = True):
    """Run Godot and return ``(returncode, output)``, both streams kept.

    ``headless`` is the scripted default, so the claim needs no display. It is *not* safe
    while a headset is streaming over Quest Link: a headless engine has no swapchain, and
    the measured result is an immediate access violation (3 of 3 runs, exit 0xC0000005,
    before the engine printed anything at all). Callers retry on a display surface.
    """
    env = dict(os.environ)
    env["SHUGOCORE_XR_AGENT_URL"] = url
    env.setdefault("SHUGOCORE_XR_POLL_SECONDS", "0.5")
    argv = [godot, *(["--headless"] if headless else []), "--path", project, *extra]
    try:
        proc = subprocess.run(  # noqa: S603 - our own tool, vector args, no shell
            argv, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, env=env,
            shell=False)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        return 124, (out if isinstance(out, str) else "")
    except Exception as exc:                    # pragma: no cover - spawn failure
        return 126, f"{type(exc).__name__}: {exc}"
    return int(proc.returncode or 0), (proc.stdout or "") + (proc.stderr or "")


def start_server(args, port: int):
    """Start the desktop server the bridge talks to, in its own process and data dir."""
    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "shugocore_server.py", "--host", "127.0.0.1",
            "--port", str(port), "--backend", args.backend, "--model", args.model,
            "--memory-db-path", str(data / "semantic_memory.db"),
            "--audit-path", str(data / "audit_chain.jsonl"),
            "--no-mobile"]
    if args.backend_url:
        argv += ["--backend-url", args.backend_url]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        return subprocess.Popen(  # noqa: S603 - our own tool, vector args, no shell
            argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, shell=False)
    except Exception as exc:
        print(f"[SESSION] could not start the server: {type(exc).__name__}: {exc}")
        return None


def wait_for_server(port: int, budget: float = 60.0) -> bool:
    """Poll /health until the server answers, or give up. Readiness is observed, not assumed."""
    import urllib.request

    deadline = time.monotonic() + budget
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as reply:
                if 200 <= reply.status < 300:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


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


def world_lines(output: str) -> list:
    """The session's own world-engagement lines, in the order it printed them."""
    return [line.strip() for line in (output or "").splitlines()
            if any(line.lstrip().startswith(key) for key in WORLD_KEYS)]


def transcript_lines(facts: dict) -> list:
    """The proof as lines. The scaffold's own output is quoted; nothing is embellished."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    lines = [f"world session: {stamp}  surface=godot scaffold  "
             f"agent=http://127.0.0.1:{facts.get('port')} "
             f"(backend {facts.get('backend')})"]
    if facts.get("godot"):
        lines.append(f"surface binary: {facts['godot']}")
    if facts.get("imported"):
        lines.append(f"project import: {facts['imported']}")
    if facts.get("error"):
        lines.append(f"error={facts['error']}")
    for line in facts.get("world") or []:
        lines.append(line)
    if facts.get("rc") is not None:
        lines.append(f"surface exit: {facts['rc']}")
    if facts.get("tail"):
        lines.append("surface said (last lines):")
        lines.extend(f"    {line}" for line in facts["tail"])
    lines.append("verdict: world={world} presence={presence} acted={acted} replied={replied}".format(
        world="xr" if facts.get("world") else "none",
        presence=facts.get("presence", "unknown"),
        acted=bool(facts.get("acted")), replied=bool(facts.get("replied"))))
    return lines
def scan_world(facts: dict) -> None:
    """Read the session's own result out of its own lines: acted, replied, presence."""
    facts.setdefault("acted", False)
    facts.setdefault("replied", False)
    for line in facts.get("world") or []:
        if line.startswith("[PRESENCE]"):
            facts["presence"] = line.split("mode=", 1)[-1].strip()
        elif line.startswith("[ACTION"):
            facts["acted"] = True
        elif line.startswith("[REPLY"):
            facts["replied"] = True


def session_log_path(out: str) -> str:
    """Where the surface writes its own session log (absolute: Godot resolves it itself)."""
    return str(Path(os.path.dirname(out) or ".").resolve() / "world.xr.session.log")


def read_session_log(path: str, since: float = 0.0) -> list:
    """The lines the surface wrote *during this run*, if it got that far.

    ``since`` guards against reading a previous run's transcript as this run's evidence. The
    surface writes its own log file, and a session that dies before opening it leaves the
    last run's file on disk -- measured: a crashed run reported a transcript from twenty
    minutes earlier, complete with a presence mode it had never observed.
    """
    try:
        if since and Path(path).stat().st_mtime < since:
            return []
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return world_lines(text)


def write_and_report(args, facts: dict, code: int) -> int:
    lines = transcript_lines(facts)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The surface's raw output, kept beside the transcript: the transcript quotes it, and when
    # the session fails this is the only place the reason survives.
    raw = Path(os.path.dirname(args.out) or ".") / "world.xr.surface.log"
    raw.write_text(facts.get("raw") or "", encoding="utf-8")
    for line in lines:
        print(line, flush=True)
    return code


def _describe_exit(code: int) -> str:
    """Plain language for the exit codes this surface actually produces on Windows.

    ``surface exited (4294967295)`` says nothing to an operator, and an access violation
    arriving as a large number looks like a mystery where the truth is known -- so the two
    codes that matter are named rather than printed raw.
    """
    if code == 0:
        return "exited cleanly"
    if code == 130:
        return "stopped by you"
    if code in (3221225477, -1073741819):
        return ("crashed with an access violation (0xC0000005) -- the engine's teardown "
                "with a live XR session, not the scaffold")
    if code in (4294967295, -1):
        return "was terminated"
    return f"exited with code {code}"


def run_interactive(args, godot: str, project: str, url: str, port: int) -> int:
    """Hand the scaffold to the operator: a real session, no budget, no auto-quit.

    Three differences from the scripted path, each measured rather than chosen:

      * **no ``--headless``.** A headless engine has no swapchain to hand the compositor,
        so it cannot reach a real XR session at all. Found by running it the other way:
        with ``--headless`` the engine failed with
        ``XR_ERROR_GRAPHICS_REQUIREMENTS_CALL_MISSING``, and the same scene run without
        the flag reached ``XR_SESSION_STATE_FOCUSED`` and rendered to the headset.
      * **no ``--quit-after``**, and ``--linger`` on the session, so the surface stays up
        while the operator wears it instead of exiting when the first exchange completes.
        A few seconds of frame budget is what "it only ran for a few moments" was.
      * **stdio inherited, not captured**, so the scaffold's own log is visible live
        instead of only after it exits.

    Nothing here is judged: the scripted path stays the one the claims matrix reads.
    """
    log_path = INTERACTIVE_LOG
    env = dict(os.environ)
    env["SHUGOCORE_XR_AGENT_URL"] = url
    env.setdefault("SHUGOCORE_XR_POLL_SECONDS", "0.5")
    extra = ["--path", project, "--", "--world-session", "--linger",
             f"--agent-url={url}", f"--session-out={log_path}"]
    print(f"[SESSION] interactive — the scaffold is live against {url}")
    print(f"[SESSION] session log: {log_path}")
    print("[SESSION] put the headset on; Ctrl+C here stops it")
    try:
        proc = subprocess.Popen(  # noqa: S603 - our own tool, vector args, no shell
            [godot, *extra], cwd=str(ROOT), env=env, shell=False)
    except Exception as exc:
        print(f"[SESSION] could not start the surface: {type(exc).__name__}: {exc}")
        return 1
    try:
        code = int(proc.wait())
    except KeyboardInterrupt:
        print("\n[SESSION] interrupted — stopping the surface")
        stop_process(proc)
        code = 130
    lines = read_session_log(log_path)
    print(f"[SESSION] surface {_describe_exit(code)}; it recorded {len(lines)} line(s):")
    for line in lines:
        print("    " + line)
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    project = str(Path(args.project))
    facts = {"godot": "", "port": args.port or 0, "backend": args.backend, "world": [],
             "rc": None, "tail": [], "error": "", "imported": "", "acted": False,
             "replied": False, "presence": "unknown"}
    godot = find_godot(args.godot)
    if not godot:
        facts["error"] = ("no Godot binary: pass --godot, set $SHUGOCORE_GODOT, or put one "
                          "on PATH")
        return write_and_report(args, facts, 1)
    facts["godot"] = godot
    port = args.port or free_port()
    facts["port"] = port
    url = f"http://127.0.0.1:{port}"
    server = None
    try:
        # Import first when Godot has never scanned the project: without it the scaffold's own
        # class_name globals do not resolve, its autoloads fail to load, and the scaffold
        # cannot reach anything -- which looks exactly like an unreachable agent.
        if not project_imported(Path(project)):
            rc, output = run_godot(godot, project, ["--import"], "", 900.0)
            facts["imported"] = f"godot --import exit={rc}"
            if rc != 0:
                facts["imported"] += f" ({output.strip()[:200]})"
        server = start_server(args, port)
        if server is None:
            facts["error"] = "the desktop server did not start"
            return write_and_report(args, facts, 1)
        if not wait_for_server(port):
            facts["error"] = "the desktop server never answered /health"
            return write_and_report(args, facts, 1)
        if args.interactive:
            # The operator path: no frame budget, no --headless, no transcript written.
            # It returns here rather than falling through, so the scripted run below keeps
            # producing the evidence the claims matrix reads.
            return run_interactive(args, godot, project, url, port)
        log_path = session_log_path(args.out)
        # ``--quit-after`` is frames, and it is the belt to the harness's braces: the session
        # asks the tree to quit when it is done, but an engine that is asked to quit from an
        # autoload has been observed to keep running here, so the surface is given a hard
        # frame budget just past its own deadline as well.
        frames = max(900, int(args.timeout * 60))
        world_args = ["--quit-after", str(frames), "--", "--world-session",
                      f"--agent-url={url}", f"--session-out={log_path}"]
        # Recorded so the transcript cannot come from a previous run's log file.
        started = time.time()
        rc, output = run_godot(godot, project, world_args, url, args.timeout)
        facts["rc"] = rc
        facts["raw"] = output
        if not (read_session_log(log_path, started) or world_lines(output)) \
                and rc not in (0, 124):
            # The headless surface died without printing a line -- with a headset streaming
            # over Quest Link that is exactly what happens (measured 3 of 3: access
            # violation before any output), so the same scripted session is retried on a
            # display surface. Once, and loudly: a silent retry would hide the crash.
            print(f"[SESSION] the headless surface exited {rc} with no transcript; "
                  "retrying with a display surface")
            started = time.time()
            rc, output = run_godot(godot, project, world_args, url, args.timeout,
                                   headless=False)
            facts["rc"] = rc
            facts["raw"] = output
        # Prefer the surface's own log: Godot buffers print() to stdout when it is piped, so a
        # killed run can lose every line the surface printed while its log file kept them.
        facts["world"] = read_session_log(log_path, started) or world_lines(output)
        kept = [line.strip() for line in output.splitlines() if line.strip()]
        facts["tail"] = kept[-6:]
        scan_world(facts)
        if not facts["world"]:
            facts["error"] = "the scaffold printed no world session"
        good = bool(facts["world"]) and facts["acted"] and facts["replied"]
        return write_and_report(args, facts, 0 if good else 1)
    finally:
        stop_process(server)


if __name__ == "__main__":
    raise SystemExit(main())

