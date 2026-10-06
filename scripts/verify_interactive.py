#!/usr/bin/env python3
"""Drive one real node turn-by-turn and report what actually backed each reply.

The operator terminal (`clients/desktop/shugocore_desktop.py --terminal`) is the
interactive surface; this is the *verification* of it, so a claim that "you can
talk to the agent" has evidence rather than a session someone watched. It builds
the node the way the terminal does (``create_agent``, a data dir, the engine's
registry re-pointed at the chosen endpoint, a real speak listener), warms the
endpoint, then submits typed turns through the agent's own seam
(``handle_typed_input``) -- the same path a keyboard turn takes.

For every turn it prints three things and invents nothing between them:

    what was typed          the exact string handed to the agent
    what came back          the agent's reply, or NO REPLY (silence)
    what backed it          the decision's proposal_source -- the model id when
                            a model answered, a rule name when one stood in,
                            "nothing" when the node never decided

The exit code is about the *harness*: 1 when a turn produced no reply at all
(that is the defect this exists to catch -- a silent agent), 0 otherwise. A
rule-backed reply is reported, not failed: a deterministic answer to "what time
is it" is the correct behaviour, and the summary counts model-backed turns
separately so the two are never conflated.

    python scripts/verify_interactive.py                       # offline stub
    python scripts/verify_interactive.py \
        --url http://127.0.0.1:1234 --model zai-org/glm-4.6v-flash
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

# Ordinary operator sentences. The point is not the answer -- it is that a turn
# produces *something* and says what produced it, so the wording stays plain.
DEFAULT_TURNS = [
    "hello, who are you?",
    "what can you do?",
    "what time is it?",
    "remember that my name is Ada",
]

# Sources that mean a rule, not a model, stood in for the decision. Mirrors
# `AndroidAgent._model_backed` and `claim_matrix`; kept as one list here so the
# three cannot disagree about what "model-backed" means.
STOOD_IN = {"", "none", "rule_fallback", "null_proposal", "proposer_backoff",
            "conversation_fallback", "conversation_empty", "operator_test",
            "conversation_raw", "conversation_unparseable",
            "conversation_rerouted"}


class Speaker:
    """The agent's real speak listener: what a human at this node would hear."""

    def __init__(self):
        self.spoken = []

    def speak(self, text):
        text = str(text or "").strip()
        if text:
            self.spoken.append(text)
        return True


def warm(endpoint: str, model: str, budget: float = 300.0) -> str:
    """Ask the endpoint to answer once, and say what that cost.

    A server that loads weights on its first request answers slowly -- or with
    an error while it loads -- and behaves only afterwards. That is the machine's
    state, not the node's reasoning, so the precondition is established before
    anything is measured. Every candidate name is tried for an OpenAI-compatible
    endpoint, because a listed model is not necessarily a loadable one (measured
    on this machine: a 27B that asks for 64.74 GB sits beside the one that works).
    """
    try:
        import requests
    except Exception as exc:
        return f"cannot probe the endpoint ({type(exc).__name__})"
    base = str(endpoint or "").rstrip("/")
    if base and not base.endswith("/v1"):
        base += "/v1"
    deadline = time.monotonic() + max(0.0, float(budget))
    candidates, listed = ([model] if model else []), []
    attempt, last = 0, "no attempt was possible"
    while True:
        attempt += 1
        try:
            data = requests.get(f"{base}/models", timeout=10).json()
            listed = [str(e.get("id") or "") for e in (data.get("data") or [])]
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        for name in candidates or [n for n in listed if "embed" not in n.lower()]:
            if not name:
                continue
            try:
                reply = requests.post(
                    f"{base}/chat/completions", timeout=300,
                    json={"model": name, "temperature": 0.0, "max_tokens": 4,
                          "messages": [{"role": "user", "content": "ok"}]})
                if reply.status_code < 400:
                    return f"warm after {attempt} attempt(s) (model {name})"
                last = f"model '{name}' -> HTTP {reply.status_code}"
            except Exception as exc:
                last = f"model '{name}' -> {type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            return f"NOT warm after {attempt} attempt(s): {last}; running anyway"
        time.sleep(3.0)


def isolate_from_mesh(agent) -> str:
    """Stop the mesh so this node is the only device that can answer.

    A verifier must measure *this* node, and a machine with live ShugoCore nodes
    on its LAN does not: they advertise perception facts, so the response router
    legitimately picks one of them as the closest mouth and delegates the reply
    there -- which is correct hive behaviour and useless as evidence about the
    node under test. Isolating makes the run deterministic (the router has only
    this node to choose), and `--allow-mesh` opts back out.
    """
    runtime = getattr(agent, "shugonet_runtime", None)
    try:
        if runtime is not None:
            runtime.stop()
    except Exception:
        pass
    agent.shugonet_runtime = None
    if isinstance(getattr(agent, "telemetry", None), dict):
        agent.telemetry["mesh_peers"] = []
    if isinstance(getattr(agent, "last_observation", None), dict):
        agent.last_observation.pop("mesh_peers", None)
    # Heartbeats already received would still name a peer as lease holder, which
    # would send this node down the forwarding path for a device that is no
    # longer part of the run. Drop them so the election sees a lone node.
    election = getattr(agent, "mesh_election", None)
    if election is not None:
        try:
            peers = election.live_peers() or {}
            for node_id in (peers.keys() if isinstance(peers, dict)
                            else [p[0] for p in peers]):
                election.drop_node(node_id)
        except Exception:
            pass
    return "isolated (single device decides)"


def build_node(args):
    """One real node, wired the way the operator terminal wires it."""
    from shugocore_agent import create_agent

    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)
    agent = create_agent(device_caps=args.device_caps,
                         api_url=(args.url or None),
                         data_dir=data_dir,
                         node_role="primary-capable")
    if agent.engine is None:
        return agent, None

    # Register the model the endpoint actually serves. The engine bootstraps
    # with the on-device placeholder id, which a host backend does not know: it
    # answers 404 and the only symptom is a rule fallback every cycle.
    if args.backend == "stub":
        config = {"type": "stub"}
    else:
        config = {"type": "openai", "base_url": str(args.url or "").rstrip("/"),
                  "json_mode": args.json_mode}
    model_id = args.model or "shugocore-local"
    agent.engine.models = [{"id": model_id, "type": "text", "weight": 1.0,
                            "backend": config}]
    agent.engine.model_manager.models = agent.engine.models
    agent.engine.model_manager.model_performance = {model_id: 1.0}
    agent.engine._backend_cache.clear()
    return agent, model_id


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--backend", choices=("stub", "openai"), default="openai",
                    help="model transport; 'stub' runs entirely offline")
    ap.add_argument("--url", default="http://127.0.0.1:1234",
                    help="model endpoint base URL (no /v1)")
    ap.add_argument("--model", default="", help="model id the endpoint serves")
    ap.add_argument("--json-mode", default="json_schema",
                    help="structured-output hint for an OpenAI-compatible server")
    ap.add_argument("--data-dir", default=os.path.join("runtime", "interactive_verify"))
    ap.add_argument("--device-caps", default="desktop")
    ap.add_argument("--allow-mesh", action="store_true",
                    help="keep this node on the mesh. Off by default: a live LAN "
                         "of ShugoCore nodes legitimately out-scores this one for "
                         "response placement, which makes the run evidence about "
                         "the LAN rather than about this node")
    ap.add_argument("--turn", action="append", default=[], metavar="TEXT",
                    help="a turn to submit (repeatable; default: a short script)")
    ap.add_argument("--out", default="", help="also write the JSON report here")
    args = ap.parse_args(argv)

    if args.backend == "stub":
        print("endpoint   : offline stub (no network)")
    else:
        print(f"endpoint   : {args.url}")
        print(f"warming    : {warm(args.url, args.model)}")

    agent, model_id = build_node(args)
    if agent.engine is None:
        print("node failed to build an engine: "
              f"{str(getattr(agent, 'engine_error', ''))[:400]}", file=sys.stderr)
        return 1
    print(f"node       : {getattr(agent, 'node_id', '?')}  "
          f"(engine={type(agent.engine).__name__}, model={model_id})")
    # The speak listener is what makes this node a *response candidate*; without
    # it a reply has nowhere to be delivered and the router may not choose here.
    speaker = Speaker()
    agent.register_speak_listener(speaker)
    isolation = ("mesh left on (replies may be placed elsewhere)"
                 if args.allow_mesh else isolate_from_mesh(agent))
    print(f"mesh       : {isolation}")
    print(f"mesh role  : {agent._mesh_role_label()}\n")

    turns = args.turn or DEFAULT_TURNS
    report, silent = [], []
    for text in turns:
        agent._decision_source = ""
        before = len(speaker.spoken)
        started = time.time()
        try:
            handled = agent.handle_typed_input(text)
            error = ""
        except Exception as exc:
            handled, error = False, f"{type(exc).__name__}: {exc}"
        elapsed = time.time() - started
        replies = speaker.spoken[before:]
        source = str(getattr(agent, "_decision_source", "") or "")
        model_backed = bool(source) and source.lower() not in STOOD_IN
        report.append({"turn": text, "handled": bool(handled), "replies": replies,
                       "source": source, "model_backed": model_backed,
                       "seconds": round(elapsed, 2), "error": error})
        print(f"you   > {text}")
        if error:
            print(f"        !! {error}")
        if not replies:
            silent.append(text)
            print("agent > *** NO REPLY ***")
        for line in replies:
            print(f"agent > {line}")
        print(f"        [{source or 'nothing'} | model_backed={model_backed}"
              f" | {elapsed:.1f}s]\n")

    answered = len(report) - len(silent)
    print("=== summary ===")
    print(f"turns submitted  : {len(report)}")
    print(f"turns answered   : {answered}")
    print(f"silent turns     : {len(silent)}"
          + (f"  ({'; '.join(silent)})" if silent else ""))
    print(f"model-backed     : {sum(1 for r in report if r['model_backed'])}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"report           : {args.out}")
    try:
        agent.shutdown()
    except Exception:
        pass
    return 1 if silent else 0


if __name__ == "__main__":
    raise SystemExit(main())
