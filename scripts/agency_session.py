#!/usr/bin/env python3
"""A scripted session against a real node, so an agency claim has evidence.

``claim_matrix.py`` judges a claim two ways: a command that exits zero, and a *pure
parser* over captured text. This is the producer for the second kind. It builds the node
the way a phone does (``create_agent``, with a data dir so the journals, timers and the
personality persist), drives it through the same typed-turn seam the operator terminal
uses, ticks the real loop, and writes two files the parsers read:

    runtime/evidence/agency/<scenario>.log   the transcript, in both directions
    runtime/evidence/agency/<scenario>.json  the node's own status snapshot

Scenarios ask about agency rather than about plumbing:

    timed        does the node act later, unprompted, on a goal it accepted now?
    memory       does a fact survive a *restart* of the process?
    personality  does the node grow a generation after a window of turns?
    perception   does a phrase it *heard* (not typed) cause an action and a reply?
    status       what does the node report about its own pipelines?

Nothing here fabricates. Every line is the operator's input, the agent's reply, a log line
the agent emitted, or a fact about the run itself -- and a scenario that cannot run says so
and still writes its log, because "we could not show this" is a result too. The exit code is
about the *harness*: 1 means the session could not be run, never that the node failed to be
agentic. That verdict belongs to the parser, in the matrix.

    python scripts/agency_session.py --scenario timed
    python scripts/agency_session.py --scenario memory --phase second
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = os.path.join("runtime", "evidence", "agency")
DEFAULT_DATA = os.path.join("runtime", "agency_node")
TICK_S = 0.4

# Ordinary operator sentences: the scenarios check what comes back rather than asking for a
# particular answer, so the node is never prompted into performing for the matrix.
GREETINGS = ("hello", "how are you", "what can you do", "thank you", "good morning")


def warm_model(api_url: str, model: str = "", budget: float = 180.0) -> str:
    """Ask the model endpoint to answer once, and wait (bounded) until it does.

    A server that loads its weights on the first request answers slowly -- or with an error
    while it loads -- and behaves only afterwards. That is the machine's state, not the node's
    reasoning, so the model-backed scenario establishes the precondition it is about (a model
    that *can* answer) before it measures anything, and says in the transcript what the wait
    cost.

    Every candidate name is tried, not just the first one the server lists: measured on this
    machine, ``/v1/models`` lists a second 27B that *cannot* load (it asks for 64.74 GB) beside
    the one that can, and the listing order is not stable -- so "warm models[0]" warmed a model
    that answers HTTP 400 with its own reason, and reported the endpoint as cold while a
    perfectly good model sat next to it.
    """
    try:
        import requests
    except Exception as exc:
        return f"cannot probe the endpoint ({type(exc).__name__})"
    base = str(api_url or "").rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    deadline = time.monotonic() + max(0.0, float(budget))
    attempt, last = 0, "no attempt was possible"
    while True:
        attempt += 1
        try:
            listing = requests.get(f"{base}/models", timeout=10).json()
            listed = [str(entry.get("id") or "") for entry in (listing.get("data") or [])]
            chat_capable = [name for name in listed
                            if name and "embed" not in name.lower()]
        except Exception as exc:
            chat_capable = []
            last = f"{type(exc).__name__}: {exc}"
        # The node's own default alias first (LM Studio resolves it to whatever is loaded),
        # then every chat-capable id the server lists. An explicitly named model wins outright.
        candidates = ([str(model)] if model else ["shugocore-local"] + chat_capable)
        for name in candidates:
            if not name:
                continue
            try:
                # A cold 27B can take minutes to load, and loading is the whole point of
                # this call: the node's own 120s timeout is right for a *serving* model but
                # too short to bring one up, so a shorter timeout here reported the
                # endpoint cold while it was still loading.
                #
                # The wait has to stay inside the budget, though. A flat timeout=300
                # inside a 180s budget let a single hung attempt outlive the budget
                # entirely -- the deadline below is only checked once the call returns.
                # Measured: that is how this scenario outlived the claims runner's own
                # 300s cap and was killed with no transcript to judge.
                reply = requests.post(
                    f"{base}/chat/completions",
                    timeout=max(1.0, deadline - time.monotonic()),
                    json={"model": name, "temperature": 0.0, "max_tokens": 1,
                          "messages": [{"role": "user", "content": "ok"}]})
                if reply.status_code < 400:
                    return (f"warm after {attempt} attempt(s) "
                            f"(HTTP {reply.status_code}, model {name})")
                last = f"model '{name}' -> HTTP {reply.status_code}: {str(reply.text)[:200]}"
            except Exception as exc:
                last = f"model '{name}' -> {type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            return (f"NOT warm after {attempt} attempt(s) in {budget:.0f}s "
                    f"({last}); running anyway")
        time.sleep(3.0)


class Session:
    """One scripted session: the transcript, the node, and the ticking between them."""

    def __init__(self, scenario, data_dir, out_dir, node_role="primary-capable",
                 device_caps="desktop", append=False):
        self.scenario = scenario
        self.data_dir = data_dir
        self.device_caps = device_caps
        self.role = node_role
        os.makedirs(out_dir, exist_ok=True)
        self.log_path = os.path.join(out_dir, f"{scenario}.log")
        self.json_path = os.path.join(out_dir, f"{scenario}.json")
        self.agent = None
        self.spoken = []
        self._seq = 0
        # Append when this run is the *second* half of a restart scenario: the artifact has
        # to hold the whole story -- the fact, the process ending, the fact coming back --
        # not just the half that happened to run last.
        self._handle = open(self.log_path, "a" if append else "w", encoding="utf-8")

    # -- the transcript ----------------------------------------------------
    def log(self, line) -> None:
        text = str(line)
        self._handle.write(text + "\n")
        self._handle.flush()             # a session that dies still leaves evidence
        print(text, flush=True)          # and a capturing runner sees it too

    def close(self) -> None:
        try:
            self._handle.close()
        except Exception:
            pass

    def drain_agent_logs(self) -> None:
        """Whatever the node said to itself, in order, into the transcript."""
        try:
            payload = json.loads(self.agent.recent_logs_json(self._seq) or "[]")
        except Exception:
            return
        if not isinstance(payload, list):
            return
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            try:
                seq = int(entry.get("seq") or entry.get("id") or 0)
            except Exception:
                seq = 0
            if seq > self._seq:
                self._seq = seq
            category = entry.get("category") or entry.get("cat") or ""
            message = entry.get("message") or entry.get("msg") or ""
            self.log(f"[AGENT  ] [{category}] {message}")

    # -- the node ----------------------------------------------------------
    def build(self):
        """The same factory a phone calls, with a data dir so state persists."""
        from shugocore_agent import create_agent
        # Absolute, deliberately: the node chdirs into its data dir during boot, so a
        # relative path is joined onto itself (runtime/agency_node/runtime/agency_node/),
        # the decision log cannot be created, and memory init fails with it.
        self.data_dir = os.path.abspath(self.data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.agent = create_agent(device_caps=self.device_caps,
                                  data_dir=self.data_dir,
                                  node_role=self.role,
                                  local_model=False)
        self.agent.register_speak_listener(self)
        self.log(f"[SESSION] scenario={self.scenario} node=shugo-{self.device_caps} "
                 f"role={self.role} data-dir={os.path.abspath(self.data_dir)}")
        return self.agent

    def speak(self, text) -> bool:
        """The agent's speak listener: proof the node said something out loud."""
        self.spoken.append(str(text))
        self.log(f"[SPEAK  ] {text}")
        return True

    def tick(self, seconds, stop_when=None) -> bool:
        """Run the real loop for a while, draining what the node says as it goes.

        ``stop_when`` lets a scenario stop on the event it is waiting for, so a session
        lasts as long as the behaviour needs rather than as long as a sleep says.
        """
        deadline = time.monotonic() + float(seconds)
        while time.monotonic() < deadline:
            try:
                self.agent.tick()
            except Exception as exc:
                self.log(f"[TICK   ] tick failed: {type(exc).__name__}: {exc}")
                return False
            self.drain_agent_logs()
            if stop_when is not None and stop_when():
                return True
            time.sleep(TICK_S)
        return True

    def say(self, text, label="typed") -> None:
        """One typed turn, through the agent's own conversational path."""
        self.log(f"[TURN   ] {label}: {text}")
        try:
            self.agent.handle_typed_input(text)
        except Exception as exc:
            self.log(f"[TURN   ] failed: {type(exc).__name__}: {exc}")
        self.drain_agent_logs()

    def heard(self, text) -> None:
        """One phrase the node *heard*: the fleet's own speech observation."""
        self.log(f"[HEARD  ] {text}")
        try:
            result = self.agent.publish_human_observation(
                {"type": "speech", "source": "on_device_stt",
                 "payload": {"transcript": text}})
        except Exception as exc:
            self.log(f"[HEARD  ] failed: {type(exc).__name__}: {exc}")
            return
        detail = result if isinstance(result, dict) else {}
        self.log(f"[HEARD  ] accepted={detail.get('accepted')} "
                 f"type={detail.get('type')} detail={detail.get('detail')}")
        self.drain_agent_logs()

    def snapshot(self) -> None:
        """The node's own status, as it reports it, kept verbatim."""
        try:
            raw = self.agent.get_status_json() or "{}"
        except Exception as exc:
            raw = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        with open(self.json_path, "w", encoding="utf-8") as handle:
            handle.write(raw)
        try:
            status = json.loads(raw)
        except Exception:
            return
        if not isinstance(status, dict):
            return
        for key in ("node_id", "mesh_role", "model", "security_baseline",
                    "personality", "pipeline"):
            if key in status:
                self.log(f"[STATUS ] {key}={json.dumps(status[key])}")


# -- the scenarios ---------------------------------------------------------

def scenario_timed(session, args):
    """Accept a goal now; act on it later, with nobody typing in between."""
    session.say("remind me in two seconds to check the battery")
    stopped = session.tick(9.0, stop_when=lambda: any(
        "done" in str(text).lower() for text in session.spoken))
    session.log(f"[SESSION] speaks={len(session.spoken)} early_stop={stopped}")


def scenario_memory(session, args):
    """A fact, then the same fact once the process has gone and come back."""
    if args.phase == "first":
        session.say("remember that my sister's name is Ana")
        session.tick(1.5)
        session.log("[SESSION] phase=first: the process ends here")
    else:
        session.log("[SESSION] phase=second: a new process, the same data dir")
        session.say("what is my sister's name")
        session.tick(1.5)


def scenario_personality(session, args):
    """A generation is made of a window of turns, so here is a window."""
    every = int(getattr(session.agent, "GROWTH_EVERY", 25) or 25)
    for index in range(every + 2):
        session.say(GREETINGS[index % len(GREETINGS)])
    session.tick(2.0)
    session.log(f"[SESSION] turns={every + 2} (GROWTH_EVERY={every})")


def scenario_perception(session, args):
    """Words it heard, rather than words it was handed."""
    session.heard("what is the battery level")
    session.tick(5.0, stop_when=lambda: bool(session.spoken))


def scenario_status(session, args):
    """What the node says about its own pipelines, in its own words."""
    session.tick(1.5)


def scenario_sandbox(session, args):
    """Run the actuation sandbox, and keep its transcript.

    This is the stack that reaches for optional pieces (vector search among them), so it is
    where a privacy claim has to be read: whatever it tries to reach, it says so in its own
    output -- including the failure to reach it.
    """
    import subprocess
    command = [sys.executable, os.path.join(str(ROOT), "actuation_sandbox.py"),
               "--data-dir", os.path.join("runtime", "sandbox")]
    session.log(f"[SANDBOX] {' '.join(command)}")
    try:
        proc = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                              timeout=args.timeout)
    except Exception as exc:
        session.log(f"[SANDBOX] could not run: {type(exc).__name__}: {exc}")
        return
    for stream in (proc.stdout or "", proc.stderr or ""):
        for line in stream.splitlines():
            session.log(f"[SANDBOX] {line}")
    session.log(f"[SANDBOX] exit={proc.returncode}")


def scenario_model(session, args):
    """The operator terminal itself, against whatever model server this machine runs.

    Nothing is simulated: it runs the real console as a subprocess, lets it take a real turn,
    and keeps both streams. What the transcript has to show is the *backing* -- a model id,
    not the name of a rule standing in for one -- because that is the difference between a
    node whose reasoning is connected and one whose fallback answered. The endpoint defaults to
    LM Studio on this machine and is overridable, since the wire is OpenAI-compatible and the
    same run works against llama.cpp, vLLM or a ShugoCore server.
    """
    import subprocess
    # Establish the precondition this scenario is about before measuring it: a model that can
    # answer. Recorded either way, so a reader sees what was waited for and what it cost.
    session.log(f"[SESSION] warming the model endpoint at {args.api_url} "
                f"(budget {args.warm_seconds:.0f}s)")
    session.log(f"[SESSION] warm-up: "
                f"{warm_model(args.api_url, args.model, args.warm_seconds)}")
    command = [sys.executable, os.path.join(str(ROOT), "clients", "desktop",
                                            "shugocore_desktop.py"),
               "--terminal", "--backend", args.backend, "--url", args.api_url,
               "--no-input", "--data-dir", args.model_data_dir,
               "--mesh-port", str(args.mesh_port),
               "--say", args.prompt, "--exit-after", str(args.hold)]
    if args.model:
        command += ["--model", args.model]
    session.log(f"[TERMINAL] {' '.join(command)}")
    try:
        proc = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                              timeout=args.timeout)
    except Exception as exc:
        session.log(f"[TERMINAL] could not run: {type(exc).__name__}: {exc}")
        return
    for stream in (proc.stdout or "", proc.stderr or ""):
        for line in stream.splitlines():
            session.log(f"[TERMINAL] {line}")
    session.log(f"[TERMINAL] exit={proc.returncode}")


SCENARIOS = {
    "timed": scenario_timed,
    "memory": scenario_memory,
    "personality": scenario_personality,
    "perception": scenario_perception,
    "status": scenario_status,
    "sandbox": scenario_sandbox,
    "model": scenario_model,
}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--phase", default="first", choices=("first", "second"),
                    help="memory only: run twice against one data dir to restart")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--node-role", default="primary-capable",
                    help="a follower will not answer, which is itself a result")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="how long a scenario may take before it is called a harness "
                         "failure (the sandbox scenario runs another runnable)")
    # Universal on purpose: the model wire is OpenAI-compatible, so the same run works
    # against LM Studio (the default here), llama.cpp's server, vLLM or a ShugoCore server.
    ap.add_argument("--api-url", default=os.environ.get("SHUGOCORE_MODEL_URL",
                                                        "http://127.0.0.1:1234"))
    ap.add_argument("--backend", default=os.environ.get(
        "SHUGOCORE_MODEL_BACKEND", "LM Studio (OpenAI-compatible)"))
    ap.add_argument("--model", default=os.environ.get("SHUGOCORE_MODEL", ""))
    ap.add_argument("--prompt", default=("the charger is warm to the touch; note whether that "
                                         "warrants attention"),
                    help="an ordinary operator sentence that must be *decided*, not one a "
                         "built-in command answers: the battery question was answered by the "
                         "node's own command router, so the transcript showed a rule where "
                         "this claim is about the model")
    ap.add_argument("--hold", type=float, default=180.0,
                    help="how long the terminal may take one model-backed turn")
    ap.add_argument("--warm-seconds", type=float, default=180.0,
                    help="how long the model scenario waits for the endpoint to answer "
                         "once before it starts measuring (0 skips the wait)")
    ap.add_argument("--mesh-port", type=int, default=9021)
    ap.add_argument("--model-data-dir", default=os.path.join(DEFAULT_DATA, "model"))
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    session = Session(args.scenario, args.data_dir, args.out, node_role=args.node_role,
                      append=(args.scenario == "memory" and args.phase == "second"))
    code = 0
    try:
        session.build()
        # The node configures the root logger while it boots, so a level set before it is
        # overwritten -- and its engine keeps its own handler at INFO. The transcript is the
        # evidence, so it stays curated: the node's own log lines come in through
        # recent_logs_json() with their categories, and the raw Python logging (which prints
        # each line two or three times) is turned down rather than captured.
        import logging
        logging.getLogger().setLevel(logging.WARNING)
        for noisy in ("logging_manager", "decision_engine", "memory_system",
                      "continuous_agent", "subconscious"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        SCENARIOS[args.scenario](session, args)
        session.snapshot()
    except Exception as exc:
        session.log(f"[SESSION] could not run: {type(exc).__name__}: {exc}")
        code = 1
    finally:
        try:
            if session.agent is not None:
                session.drain_agent_logs()
                for closer in ("shutdown", "cleanup"):
                    method = getattr(session.agent, closer, None)
                    if callable(method):
                        method()
                        break
        except Exception:
            pass
        session.close()
    return code


if __name__ == "__main__":
    raise SystemExit(main())


