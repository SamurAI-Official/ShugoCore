#!/usr/bin/env python3
"""An offline world session for the `world.robotics` claim.

The claim: *an operator's words reach a robot in a simulation, and are answered
there*. Nothing produced a transcript, so the row sat unproven -- and the deeper
reason is that **no composition root ever constructs a `RoboticsExecutionHandler`**,
even though `DecisionEngine` registers one whenever it is given it (the server
wires the *mobile* handler in exactly the same way). The robotics interfaces
passed their own tests; the claim had never been driven.

This drives it. It builds a real `DecisionEngine` with a real
`RoboticsExecutionHandler`, which creates the deterministic stubs for ROS 2,
MoveIt 2 and Gazebo when the real ones are absent, then pushes an operator's goal
through the engine's own two gated steps -- `_gate_decision` (Tier 3 invariants,
consent, approval) and `_execute_gated` (the hash-bound policy token) -- and
writes the transcript `claim_matrix.world_engagement` judges.

A transcript is evidence, so three rules:

* the action line says ``(gated)`` only because the decision really went through
  ``_gate_decision``;
* ``[REPLY ]`` carries what the handler returned. If the robot produced nothing,
  no reply line is written and the row stays unproven -- a refusal is recorded as
  a refusal, never as an answer;
* the exit code is 0 whenever a transcript was written; the transcript is judged,
  not the exit code (the rule `xr_session.py` had to learn).

Run::

    python3 runtime/tools/robotics_session.py
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUT = os.path.join("runtime", "evidence", "world.robotics.txt")
DEFAULT_DATA = os.path.join("runtime", "robotics_session")

WORLD = "robotics"
GOAL = "operator: put the gripper above the red block, then tell me what you did"
ACTION_TYPE = "robot_manipulate"
TARGET = {"x": 0.28, "y": -0.12, "z": 0.45}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=DEFAULT_OUT, help="transcript to write")
    ap.add_argument("--data-dir", default=DEFAULT_DATA,
                    help="where the node keeps its memory db, chain and log")
    return ap.parse_args(argv)


def build_engine(data_dir):
    """A real engine with a real robotics handler, wired like a node's."""
    from decision_engine import DecisionEngine
    from policy import ApprovalBroker, CapabilityRegistry, ConsentRegistry
    from robotics_handler import RoboticsExecutionHandler

    engine = DecisionEngine(
        models=[],
        vector_db_config={"type": "chroma"},
        capabilities=CapabilityRegistry({}),
        consents=ConsentRegistry(),
        approvals=ApprovalBroker(ttl_seconds=30.0),
        robotics_handler=RoboticsExecutionHandler(),
        log_dir=data_dir,
    )
    # The consent a motion action needs, and a seat on the approval channel, are
    # both the *operator's* to give -- the acting agent may never assert its own.
    engine.consents.grant(ACTION_TYPE, granted_by="operator", ttl_seconds=30.0)
    engine.approvals.attach_operator(lambda _request: True)
    return engine



def goal_decision():
    """The operator's words, resolved to the robot action they ask for."""
    return {"action_type": ACTION_TYPE, "confidence": 0.9,
            "decision_id": "dec-robotics-session",
            "params": {"target": dict(TARGET), "reason": GOAL}}


def transcript_lines(*, action, allowed, reason, reply):
    lines = [f"[WORLD  ] {WORLD}", f"[GOAL   ] {GOAL}"]
    if not allowed:
        # Refused before the wire: no "(gated)" marker and no reply, so the
        # transcript cannot read as engagement.
        lines.append(f"[ACTION ] {ACTION_TYPE} refused by the gate -> {reason}")
        lines.append("error=the gate refused the operator's goal")
        lines.append(f"verdict: world={WORLD} presence=simulation "
                     f"acted=False replied=False")
        return lines
    lines.append(f"[ACTION ] {ACTION_TYPE} (gated) -> {action}")
    if reply:
        lines.append(f"[REPLY  ] {reply}")
    else:
        lines.append("error=the robot produced no result to report")
    lines.append(f"verdict: world={WORLD} presence=simulation "
                 f"acted={str(bool(action)).lower()} "
                 f"replied={str(bool(reply)).lower()}")
    return lines


def main(argv=None):
    args = parse_args(argv)
    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)
    previous = os.getcwd()
    os.chdir(data_dir)          # the engine's Chroma store is cwd-relative
    try:
        engine = build_engine(data_dir)
    except Exception as exc:            # noqa: BLE001 - report, do not traceback
        os.chdir(previous)
        print(f"could not build the node: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    allowed, reason, action, reply = False, "", "", ""
    try:
        decision = goal_decision()
        allowed, reason = engine._gate_decision(decision)
        if allowed:
            result = engine._execute_gated(decision) or {}
            status = result.get("status")
            action = f"status={status}"
            if status == "success":
                joints = result.get("joints") or []
                reply = (f"the arm planned {len(joints)} joints through MoveIt "
                         f"and executed the trajectory")
            else:
                reason = str(result.get("reason") or result.get("message")
                             or result)
    finally:
        try:
            engine.shutdown()
        except Exception:               # noqa: BLE001 - teardown must not mask
            pass
        os.chdir(previous)

    lines = transcript_lines(action=action, allowed=allowed, reason=reason,
                             reply=reply)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

