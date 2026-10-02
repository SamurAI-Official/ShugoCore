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


def quest3_reach(status_text: str) -> tuple:
    """A Quest 3 reached an agent over the LAN, its task was executed, and auth held.

    Read from a transcript a headset produced: the device's own identity, a 200 from the agent
    for both a status read and a task execution, the agent's answer as the *server's* log
    recorded it, and a 401 for the same request with no token.

    The 401 is half the claim. A headset on the LAN reaching an agent that lets anyone in is
    not the same thing as a headset reaching *its* agent, and the token gate is what makes the
    difference -- so a transcript without it is not evidence for this.

    Returns ``(None, reason)`` when no headset was attached: the claim then *could not be
    evaluated* rather than being contradicted, and the row is unproven. A headset that goes to
    sleep is not a claim that failed.
    """
    body = status_text or ""
    if not re.search(r"^\[HEAD\s*\]\s*device=", body, re.MULTILINE):
        if "no Quest headset is attached" in body:
            return None, "no headset was attached, so the claim could not be evaluated"
        return False, "no headset was identified in the transcript"
    head = re.search(r"^\[HEAD\s*\]\s*device=(.+?)\s+serial=(\S+).*?ip=(\S+)",
                     body, re.MULTILINE)
    if not head:
        return False, "the headset line is not readable"
    model, serial, address = head.group(1), head.group(2), head.group(3)
    if "quest" not in model.lower():
        return False, f"the device identified itself as '{model}', which is not a Quest"
    if not re.search(r"^\[STATUS\]\s*GET\s+/api/v1/status\s*->\s*200", body, re.MULTILINE):
        return False, "the headset never got a 200 for the agent's status"
    if not re.search(r"^\[TASK\s*\]\s*POST\s+/api/v1/task\s*->\s*200", body, re.MULTILINE):
        return False, "the headset's task was not executed (no 200)"
    answer = re.search(r"^\[ANSWER\]\s*(?:the agent decided|what the agent said):\s*(\S.*)$",
                       body, re.MULTILINE)
    if not answer:
        return False, "the agent's answer was never recorded"
    if not re.search(r"^\[AUTH\s*\]\s*GET\s+/api/v1/status\s+with\s+no\s+token\s*->\s*401",
                     body, re.MULTILINE):
        return False, ("a request with no token was not refused, so it is not proven that the "
                       "agent the headset reached was gated")
    return True, (f"{model} ({serial}, {address}) reached the agent, its task ran, and the "
                  f"token gate refused an unauthenticated read")


def dev_task_ran(status_text: str) -> tuple:
    """A peer ran a task the hub named, and the hub recorded the peer's own verdict.

    Two things must hold: the *peer's own process* executed the task (its line carries the
    argv and the exit code), and the hub's recorded result agrees with it. The exit code is
    deliberately not required to be 0 -- a consistency check that finds a difference is doing
    its job, and a claim about the orchestration channel must not depend on the machine being
    pristine. What must never happen is the hub recording something the peer did not say, and
    that is what the agreement check is for.
    """
    plain = status_text or ""
    wire = re.search(r"wire payload[^:]*: (\{.*\})", plain)
    if not wire:
        return False, "no wire payload recorded"
    try:
        payload = json.loads(wire.group(1))
    except Exception:
        return False, "the wire payload is not readable"
    params = payload.get("params") or {}
    if set(params) != {"task"}:
        return False, f"the wire carried more than a task name: {sorted(params)}"
    ran = re.search(r"peer ran: .*?exit=(-?\d+) ok=(\w+)", plain)
    if not ran:
        return False, "no execution line from the peer's own process"
    exit_code, ok = int(ran.group(1)), ran.group(2)
    result = re.search(r"result from peer: status=(\w+) action_type=dev_task "
                       r"delivered=(\w+)", plain)
    if not result:
        return False, "the peer's result never reached the hub"
    status, delivered = result.group(1), result.group(2)
    expected_status = "success" if exit_code == 0 else "error"
    expected_delivered = "True" if ok == "True" else "False"
    if status != expected_status or delivered != expected_delivered:
        return False, (f"the hub recorded {status}/delivered={delivered} for the peer's own "
                       f"exit={exit_code} ok={ok}")
    return True, (f"the peer ran the named task (exit {exit_code}) and the hub recorded that "
                  f"verdict, not a friendlier one")


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


def _lines(text: str) -> List[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _speaks(text: str) -> List[str]:
    """Every line the node said out loud, in order (the session's own marker)."""
    return [line for line in _lines(text) if line.startswith("[SPEAK")]


def timer_fired(text: str) -> tuple:
    """A goal accepted now, acted on later, with nobody typing in between.

    The agency part is the *absence* of input between the two: a reminder the operator had
    to ask for again is not the node pursuing anything, so a typed turn in the gap fails
    the claim rather than being ignored by it.
    """
    lines = _lines(text)
    speaks = _speaks(text)
    accepted = next((line for line in speaks if "timer set" in line.lower()), "")
    acted = next((line for line in speaks if "is done" in line.lower()), "")
    fired = any("[TIMER]" in line and "timer fired" in line for line in lines)
    if not accepted:
        return False, "the session never had a goal accepted (no timer was set)"
    if not fired:
        return False, "a goal was accepted but no timer ever fired"
    if not acted:
        return False, "the timer fired and the node said nothing about it"
    start = lines.index(accepted)
    end = lines.index(acted)
    typed_between = [line for line in lines[start + 1:end] if line.startswith("[TURN")]
    if typed_between:
        return False, (f"the operator had to type {len(typed_between)} time(s) before the "
                       f"node acted: {typed_between[0][:60]}")
    return True, f"accepted a goal, then acted on it unprompted ({acted[:48]})"


def memory_recalled(text: str) -> tuple:
    """A fact survives the process: stored, the process ends, a new one recalls it.

    The recall is checked against the fact the *session itself* recorded, not against an
    answer this tool would like to see, and the restart marker is required so a single
    process answering its own memory cannot pass as durability.
    """
    lines = _lines(text)
    taught = [line for line in lines
              if line.startswith("[TURN") and "remember" in line.lower()]
    restart = [line for line in lines if "phase=second" in line]
    if not taught:
        return False, "the session never taught the node a fact"
    token = ""
    for word in reversed(re.findall(r"[A-Za-z][A-Za-z'’-]{2,}", taught[0])):
        if word.lower() not in ("remember", "that", "the", "and", "with", "sister",
                                "name", "typed", "turn"):
            token = word
            break
    if not token:
        return False, f"could not read a fact out of {taught[0][:60]}"
    if not restart:
        return False, "no second process ran, so nothing was tested across a restart"
    after = lines[lines.index(restart[0]):]
    answered = [line for line in after
                if line.startswith("[SPEAK") and token.lower() in line.lower()]
    if not answered:
        return False, (f"the fact {token!r} was not recalled after the restart "
                       f"(speaks after restart: {len(_speaks(''.join(after)))})")
    return True, f"recalled {token!r} in a new process: {answered[0][:60]}"


def personality_grew(text: str) -> tuple:
    """The node grew a generation, and said why.

    A generation is a window of turns, so the window marker is required too: growth that
    cannot be attributed to a window is not evidence of anything.
    """
    lines = _lines(text)
    window = [line for line in lines if "GROWTH_EVERY=" in line]
    if not window:
        return False, "no turn window was reported, so no growth cycle was run"
    grew = re.search(r"grew gen (\d+)\s*->\s*(\d+)", text)
    if grew and int(grew.group(2)) > int(grew.group(1)):
        reason = ""
        match = re.search(r"grew gen \d+\s*->\s*\d+ \(([^)]*)\)", text)
        if match:
            reason = f" ({match.group(1)[:60]})"
        return True, f"generation {grew.group(1)} -> {grew.group(2)}{reason}"
    generations = [int(value) for value in re.findall(r"gen (\d+)", text)]
    if generations and max(generations) > min(generations):
        return True, (f"generation advanced {min(generations)} -> {max(generations)} "
                      f"across {len(window)} window(s)")
    return False, (f"the node ran its window without growing "
                   f"(generations seen: {sorted(set(generations)) or 'none'})")


def heard_caused_action(text: str) -> tuple:
    """Words the node *heard* led to an action and a reply.

    The same no-input rule as the timer: the operator typed nothing, so whatever the node
    did with the phrase came from having heard it.
    """
    lines = _lines(text)
    heard = [index for index, line in enumerate(lines) if line.startswith("[HEARD")]
    if not heard:
        return False, "the session never gave the node something to hear"
    if not any("accepted=True" in line for line in lines[heard[0]:]):
        return False, "the node did not accept the speech observation"
    speaks = [index for index, line in enumerate(lines)
              if line.startswith("[SPEAK") and index > heard[0]]
    if not speaks:
        return False, "the node heard something and said nothing"
    typed_between = [line for line in lines[heard[0] + 1:speaks[0]]
                     if line.startswith("[TURN")]
    if typed_between:
        return False, ("the operator typed before the node answered, so the reply cannot "
                       f"be attributed to hearing: {typed_between[0][:60]}")
    return True, f"heard a phrase and answered it unprompted ({lines[speaks[0]][:56]})"


def pipeline_reported(text: str) -> tuple:
    """The node reports its own stages, in a vocabulary that can say "I do not know".

    The honesty here *is* the vocabulary: ``unknown`` and ``down`` have to be available
    beside ``ok``, and every organ has to appear -- a health surface that only knows how to
    paint green is how a node with no model reads as healthy.
    """
    snapshots = []
    for line in _lines(text):
        if line.startswith("[STATUS") and "pipeline=" in line:
            try:
                snapshots.append(json.loads(line.split("pipeline=", 1)[1]))
            except Exception:
                return False, f"the pipeline snapshot is not JSON: {line[:80]}"
    if not snapshots:
        return False, "the node never reported its pipeline"
    allowed = {"ok", "stale", "down", "unknown"}
    organs = {"sensors", "vision", "hearing", "speech", "model", "memory"}
    for snapshot in snapshots:
        stages = snapshot.get("stages")
        if not isinstance(stages, dict) or not stages:
            return False, "a snapshot carried no stages"
        odd = {name: value for name, value in stages.items() if value not in allowed}
        if odd:
            return False, f"a stage used vocabulary outside {sorted(allowed)}: {odd}"
        missing = organs - set(stages)
        if missing:
            return False, f"organs missing from the health surface: {sorted(missing)}"
        if snapshot.get("overall") not in allowed:
            return False, f"overall={snapshot.get('overall')!r} is outside the vocabulary"
    last = snapshots[-1]
    observed = last.get("stages", {})
    return True, ("reports " + ", ".join(f"{name}={value}"
                                         for name, value in sorted(observed.items()))
                  + f" (overall={last.get('overall')})")


def no_third_party_egress(text: str) -> tuple:
    """Nothing in the session tried to report home.

    An offline-first, privacy-hardened agent that ships analytics is contradicting its own
    claim, and it is invisible unless someone reads the transcript -- this is that reading,
    over every URL the session mentions *and* every host named in an error, because a
    library that fails to reach its analytics endpoint says so as
    ``HTTPSConnectionPool(host='us.i.posthog.com')`` rather than as a URL.
    """
    body = text or ""
    hosts = {host.lower() for host in re.findall(r"https?://([A-Za-z0-9_.\-]+)", body)}
    hosts |= {host.lower() for host in
              re.findall(r"host=['\"]([A-Za-z0-9_.\-]+)", body)}
    hosts = {re.sub(r":\d+$", "", host) for host in hosts}
    local = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}
    external = sorted(host for host in hosts
                      if host not in local and not host.endswith((".local", ".internal")))
    analytics = ("posthog", "sentry", "analytics", "mixpanel", "segment", "amplitude",
                 "datadog", "google-analytics")
    named = [host for host in external if any(word in host for word in analytics)]
    if named:
        return False, f"the session reached analytics hosts: {', '.join(named)}"
    if external:
        return True, (f"no analytics egress ({len(external)} external host(s) seen: "
                      f"{', '.join(external[:3])})")
    return True, "no external host appears in the session at all"


def model_backed(text: str) -> tuple:
    """Did a *model* take the last decision, or did a rule stand in for one?

    Read from the node's own announcement of what backed its decisions, and from the absence of
    the outcome that means the model could not be reached at all. "An engine object exists" is
    not the claim -- it is precisely the reading that made a node with no model served report
    itself healthy, so a transcript showing only rule-backed decisions fails this.
    """
    announced = re.findall(r"decisions backed by\s+([^\s]+)", text or "")
    announced += re.findall(r"backed by\s+([A-Za-z0-9_./-]+)", text or "")
    stood_in = {"", "none", "rule_fallback", "null_proposal", "proposer_backoff",
                "conversation_fallback", "conversation_empty", "operator_test"}
    if not announced:
        return False, "nothing announced what backed the decisions, so nothing was judged"
    reached = [name for name in announced if name.lower() not in stood_in]
    failed = [line.strip() for line in _lines(text) if "BACKEND_FAILURE" in line]
    if not reached:
        return False, ("every decision was backed by a rule rather than a model: "
                       f"{', '.join(sorted(set(announced)))}")
    if failed:
        return False, (f"a model answered, but {len(failed)} cycle(s) could not reach it "
                       f"({failed[0][:70]})")
    return True, f"decisions backed by {reached[-1]}"


def world_engagement(text: str) -> tuple:
    """Was the agent reached *and* answered in a world, or only wired for one?

    Judged from a session transcript in the shape a world producer writes:

        [WORLD  ] robotics
        [GOAL   ] pick up the red block
        [ACTION ] moveit_plan (gated)
        [REPLY  ] the plan is approved and running

    Two properties are required, and neither can be inferred from passing interface tests.
    The *gate* marker is one: a world that actuates without passing it is exactly what the
    containment claims exist to prevent, so an ungated action is not engagement. The *reply*
    is the other: a plan that runs without answering the operator is not an answer. A row
    whose transcript is absent stays ``unproven`` rather than proven by its wiring tests --
    which is the distinction the whole matrix turns on.
    """
    body = text or ""
    worlds = re.findall(r"^\[WORLD\s*\]\s*(\S+)", body, re.MULTILINE)
    goals = re.findall(r"^\[GOAL\s*\]\s*(.+)$", body, re.MULTILINE)
    actions = re.findall(r"^\[ACTION\s*\]\s*(.+)$", body, re.MULTILINE)
    replies = re.findall(r"^\[REPLY\s*\]\s*(.+)$", body, re.MULTILINE)
    if not worlds:
        return False, "no world session was recorded"
    world = worlds[0]
    if not goals:
        return False, f"no goal was expressed in {world}"
    if not actions:
        return False, f"a goal was expressed in {world} and nothing was done about it"
    if not replies:
        return False, f"{world} acted but never answered the operator"
    # `gated` as a word, and not as the tail of `ungated`: the substring trap is exactly how a
    # bypassed gate would have read as engagement.
    ungated = [action for action in actions
               if not re.search(r"(?<!un)gated\b", action, re.IGNORECASE)]
    if ungated:
        return False, f"an action in {world} did not pass the gate: {ungated[0][:60]}"
    return True, (f"{world}: {goals[0][:40]!r} -> {actions[0][:40]} -> {replies[0][:40]!r}")


LIVE_CHECKS: Dict[str, Callable[[str], tuple]] = {
    "hub_role": hub_role,
    "hub_imported": hub_imported,
    "dev_task_ran": dev_task_ran,
    "quest3_reach": quest3_reach,
    "phone_quiet": phone_quiet,
    "audit_chain": chain_present,
    "timer_fired": timer_fired,
    "memory_recalled": memory_recalled,
    "personality_grew": personality_grew,
    "heard_caused_action": heard_caused_action,
    "pipeline_reported": pipeline_reported,
    "no_third_party_egress": no_third_party_egress,
    "model_backed": model_backed,
    "world_engagement": world_engagement,
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
                # The live half is captured from a real fleet by a real node holding the
                # lease -- the desk node, on its own data dir, hearing its own peers.
                {"kind": "command", "argv": ["__PY__", "runtime/tools/fleet_status.py"]},
                {"kind": "live", "name": "hub_role",
                 "path": "runtime/evidence/fleet_status.txt"}]},
    {"id": "mesh.memory", "claim": "Tier 2 memory crosses the mesh",
     "doc": "README 'Memory mesh semantics'",
     "checks": [{"kind": "command", "argv": ["__PY__", "runtime/tools/fleet_status.py"]},
                {"kind": "live", "name": "hub_imported",
                 "path": "runtime/evidence/fleet_status.txt"}]},
    {"id": "fleet.dev_task",
     "claim": "the hive asks a node to run a named task on itself, and that node runs it "
              "locally and reports what happened",
     "doc": "README 'Named development tasks (dev_task)'",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "pytest",
                                             "tests/test_dev_tasks.py", "-q"]},
                # A hub and a peer, two real processes, one named task through the engine's
                # own consent + approval gate -- and the peer's own line as the evidence.
                {"kind": "command", "argv": ["__PY__", "runtime/tools/dev_task_proof.py"]},
                {"kind": "live", "name": "dev_task_ran",
                 "path": "runtime/evidence/dev_task.txt"}]},
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
    # Agency: the claims that decide whether the hive is *operable* rather than merely
    # correct. Each row runs a scripted session (scripts/agency_session.py) against a real
    # node and then judges the transcript with a pure parser, the same two-step the rest of
    # this matrix uses.
    {"id": "agency.timed_autonomy",
     "claim": "the node accepts a goal and acts on it later, with nobody typing",
     "doc": "scripts/agency_session.py (timed), tools.TimerManager, agent._check_timers",
     "checks": [{"kind": "command", "argv": ["__PY__", "scripts/agency_session.py",
                                             "--scenario", "timed"]},
                {"kind": "live", "name": "timer_fired",
                 "path": "runtime/evidence/agency/timed.log"}]},
    {"id": "agency.memory_durability",
     "claim": "a fact survives the process: stored, restarted, recalled",
     "doc": "README 'Memory architecture', scripts/agency_session.py (memory)",
     "checks": [{"kind": "command", "argv": ["__PY__", "scripts/agency_session.py",
                                             "--scenario", "memory",
                                             "--phase", "first"]},
                {"kind": "command", "argv": ["__PY__", "scripts/agency_session.py",
                                             "--scenario", "memory",
                                             "--phase", "second"]},
                {"kind": "live", "name": "memory_recalled",
                 "path": "runtime/evidence/agency/memory.log"}]},
    {"id": "agency.personality_growth",
     "claim": "a window of turns grows a generation, and the node says why",
     "doc": "personality/growth.py, shugocore_agent.GROWTH_EVERY",
     "checks": [{"kind": "command", "argv": ["__PY__", "scripts/agency_session.py",
                                             "--scenario", "personality"]},
                {"kind": "live", "name": "personality_grew",
                 "path": "runtime/evidence/agency/personality.log"}]},
    {"id": "agency.perception_to_action",
     "claim": "a phrase it heard -- not typed -- reaches the reasoning path and a reply",
     "doc": "human_interaction.py (speech observations), README 'Operator engagement "
            "terminal'",
     "checks": [{"kind": "command", "argv": ["__PY__", "scripts/agency_session.py",
                                             "--scenario", "perception"]},
                {"kind": "live", "name": "heard_caused_action",
                 "path": "runtime/evidence/agency/perception.log"}]},
    {"id": "node.pipeline_health",
     "claim": "the node reports every organ in a vocabulary that can say 'unknown'",
     "doc": "human_interaction.pipeline_health, CHANGELOG 'closed-loop validation'",
     "checks": [{"kind": "command", "argv": ["__PY__", "scripts/agency_session.py",
                                             "--scenario", "status"]},
                {"kind": "live", "name": "pipeline_reported",
                 "path": "runtime/evidence/agency/status.log"}]},
    {"id": "privacy.no_third_party_egress",
     "claim": "a session on this node reaches no analytics host",
     "doc": "README 'Verifiable embeddings & privacy hardening', vector_db.py",
     "checks": [{"kind": "command", "argv": ["__PY__", "scripts/agency_session.py",
                                             "--scenario", "sandbox"]},
                {"kind": "live", "name": "no_third_party_egress",
                 "path": "runtime/evidence/agency/sandbox.log"}]},
    {"id": "reasoning.model_backed",
     "claim": "a model takes the decisions, not a rule standing in for one",
     "doc": "model_backends.py (OpenAI-compatible), README 'Operator engagement terminal'",
     "checks": [{"kind": "command", "argv": ["__PY__", "scripts/agency_session.py",
                                             "--scenario", "model"]},
                {"kind": "live", "name": "model_backed",
                 "path": "runtime/evidence/agency/model.log"}]},
    {"id": "world.robotics",
     "claim": "an operator's words reach a robot in a simulation, and are answered there",
     "doc": "ros2_interface.py, moveit_planner.py, gazebo_simulation.py, simulation/",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_robotics",
                                             "tests.test_ros2_transport_stress",
                                             "tests.test_simulation"]},
                # No transcript exists yet for the thing this claim is about: a goal spoken to
                # a simulated robot, planned through MoveIt, executed and answered. The
                # interfaces passing their own tests is a *different* claim, so this row stays
                # unproven until a world session writes the lines -- unproven is not failed.
                {"kind": "live", "name": "world_engagement", "path": ""}]},
    {"id": "world.xr",
     "claim": "an operator in a virtual space is reached there, and answered there",
     "doc": "platforms/godot/README.md (SHUGOCORE_XR_AGENT_URL, desktop_preview)",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_xr_scaffold"]},
                # A headless session against a real desktop server: the scaffold's own
                # autoloads reach the agent and the operator's words arrive as a gated task.
                # The reply is the remaining gap -- a headless server has no speech output,
                # so `speak` there is honestly `no_output` -- and until that half is recorded
                # this row stays unproven. Unproven is not failed.
                {"kind": "command", "argv": ["__PY__", "runtime/tools/xr_session.py"]},
                {"kind": "live", "name": "world_engagement",
                 "path": "runtime/evidence/world.xr.txt"}]},
    {"id": "world.sandbox",
     "claim": "the agent proposes a sandboxed action, the gate judges it, and it answers",
     "doc": "README 'Actuation sandbox', actuation_sandbox.py",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_actuation_sandbox"]},
                {"kind": "live", "name": "world_engagement", "path": ""}]},
    {"id": "mesh.quest3",
     "claim": "the operator's headset reaches the agent over the LAN, its task is executed "
              "there, and the request is gated",
     "doc": "platforms/godot/README.md 'The headset on the desk'",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_quest_probe"]},
                # The request leaves the headset and the answer is read from the *server's*
                # log, so neither side's word for it is the evidence. Needs the headset
                # attached: with none it says so, and the row stays unproven (not failed).
                {"kind": "command", "argv": ["__PY__", "runtime/tools/quest_probe.py"]},
                {"kind": "live", "name": "quest3_reach",
                 "path": "runtime/evidence/quest3.reach.txt"}]},
    {"id": "world.desktop",
     "claim": "the operator terminal reaches the agent, and labels words honestly",
     "doc": "README 'Operator engagement terminal'",
     "checks": [{"kind": "command", "argv": ["__PY__", "-m", "unittest",
                                             "tests.test_engagement_terminal",
                                             "tests.test_terminal_voice",
                                             "tests.test_terminal_ear"]}]},
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
                         f"{'ok' if passed else 'FAILED' if passed is False else 'not run'} "
                         f"- {detail}")
            if passed is None:
                # The checker could not evaluate the claim (its instrument was absent, say),
                # which is a third state on purpose: unproven, never failed.
                if verdict == "proven":
                    verdict = "unproven"
            elif not passed:
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

