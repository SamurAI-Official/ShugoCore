"""Named development tasks: the hub names one, and the node decides what the name means.

Why this exists
---------------
The hive can already ask a peer to do exactly two things: speak through its own speaker,
or host an RPC peripheral (``DELEGATABLE_ACTIONS`` in ``shugocore_agent``). Everything else
an operator does *to* a node -- check it against the fleet's declared state, pull the repo,
run the suite, rebuild the RPC server -- happens by hand, on each machine, outside the
governance layer: no consent, no approval, no audit.

``dev_task`` makes that an ordinary gated capability, and the *shape* is the safety
property: **the hub names a task and the peer decides what that name means.** Nothing that
crosses the mesh is argv. The only thing the wire carries is a name from this registry, and
the argv for that name is written here, by hand, and is never assembled from a request --
so a model, an operator console or a peer cannot turn ``run_tests`` into
``run_tests && curl ... | sh``. ``test_a_request_carrying_a_command_is_refused_by_name`` and
``test_the_executor_has_no_parameter_for_an_argv`` are the tests that hold that line, and the
executor's signature has no parameter to pass anything else.

The argv discipline is ``fleet_deploy``'s: ``shell=False``, vector arguments, and the
process is spawned with the node's *own* interpreter (``%PY%``), never one a requester can
name. No argument is derived from a request at any point -- :func:`validate_registry` even
refuses a placeholder it did not put there, because that is what an accidental
interpolation looks like.

Host-side and peer-side are deliberately separate:

- :class:`FleetDevTaskHandler` is the hub's half. It runs as the ``fleet_dev_task`` action
  type, so the engine consent-gates it and the approval broker puts a human gate on top,
  exactly like ``fleet_deploy``.
- :func:`run_named_task` is the peer's half. It resolves the name against the *local*
  registry, checks the node actually has the tool it needs, runs it, and audits both ends.

A node without this module (a phone: it is not in the Android bundle) refuses a delegated
``dev_task`` with a reason, which is the point -- a follower can be asked, but only a node
that has the registry, the tools and the trust can act.
"""
import logging
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from policy import FLEET_ACTION_TYPES, FLEET_READ_ACTION_TYPES

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
# The hub -> engine action type: consent-gated (see policy.FLEET_ACTION_TYPES) and
# approval-gated, because a task changes the machine it runs on.
DEV_TASK_ACTION = "fleet_dev_task"
# The hub -> peer action type, delegated over the mesh on the delegation topic.
DELEGATE_ACTION = "dev_task"
# The node's own interpreter, substituted into a task's argv. It exists so a registry entry
# reads the same on every platform; it is *not* a value a requester can supply.
PY = "%PY%"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_ARGV = 24
MAX_ARG_LENGTH = 512
MAX_DETAIL = 400
DEFAULT_TIMEOUT = 300.0
MAX_TIMEOUT = 7200.0
_PLACEHOLDERS = (PY,)
# A request that carries any of these is not a name-only request. Refusing by name means the
# refusal says what arrived, so a caller learns the rule instead of guessing at it.
FORBIDDEN_PARAM_KEYS = ("argv", "args", "command", "cmd", "script", "shell", "cwd", "dir",
                        "env", "executable", "interpreter", "python", "pipeline")
# Exit codes reported for a task that never ran, so they cannot be mistaken for a task's own.
EXIT_NOT_FOUND = 127
EXIT_TIMEOUT = 124
EXIT_NO_RUNNER = 126


NAMED_TASKS: Dict[str, Dict[str, Any]] = {
    "node_consistency": {
        "description": "check this node against the fleet's declared state (repo, "
                       "llama.cpp pin, Android bundle, toolchain)",
        "argv": [PY, "scripts/node_consistency.py"],
        "needs": {"files": ["scripts/node_consistency.py"]},
        "timeout": 300.0,
        "read_only": True},
    "run_tests": {
        "description": "run the repository's test suite on this node",
        "argv": [PY, "-m", "pytest", "tests/", "-q"],
        "needs": {"files": ["tests"], "modules": ["pytest"]},
        "timeout": 1800.0,
        "read_only": False},
    "git_pull": {
        "description": "fast-forward this node's checkout to its origin",
        "argv": ["git", "pull", "--ff-only"],
        "needs": {"files": [".git"], "exes": ["git"]},
        "timeout": 180.0,
        "read_only": False},
    "build_rpc_server": {
        "description": "build the layer-split host binaries for this machine",
        "argv": [PY, "scripts/build_llama_rpc.py", "--host"],
        "needs": {"files": ["scripts/build_llama_rpc.py"]},
        "timeout": 3600.0,
        "read_only": False},
    "fleet_status": {
        "description": "listen to the hive for ten seconds and report what this node heard",
        "argv": [PY, "runtime/tools/fleet_status.py", "--seconds", "10",
                 "--out", "runtime/evidence/dev_task_fleet_status.txt"],
        "needs": {"files": ["runtime/tools/fleet_status.py"]},
        "timeout": 120.0,
        "read_only": False},
}


def validate_registry(registry: Optional[Dict[str, Any]] = None) -> List[str]:
    """Problems with the registry itself, as a list (empty means sound).

    One property is being defended: apart from our own interpreter placeholder, an argv is a
    constant. A stray ``{``, ``%`` or ``$`` is how a request value would get in by accident,
    so it is reported here rather than executed later.
    """
    registry = NAMED_TASKS if registry is None else registry
    problems: List[str] = []
    for name, spec in registry.items():
        if not isinstance(name, str) or not NAME_PATTERN.match(str(name)):
            problems.append(f"task name '{name}' is not lowercase_with_underscores")
            continue
        if not isinstance(spec, dict):
            problems.append(f"task '{name}' is not a mapping")
            continue
        if not str(spec.get("description") or "").strip():
            problems.append(f"task '{name}' has no description")
        argv = spec.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv:
            problems.append(f"task '{name}' has no argv")
            continue
        if len(argv) > MAX_ARGV:
            problems.append(f"task '{name}' argv is longer than {MAX_ARGV} elements")
        for index, item in enumerate(argv):
            if not isinstance(item, str) or not item.strip():
                problems.append(f"task '{name}' argv[{index}] is not a non-empty string")
                continue
            if len(item) > MAX_ARG_LENGTH:
                problems.append(f"task '{name}' argv[{index}] is longer than "
                                f"{MAX_ARG_LENGTH} characters")
            if "\x00" in item or "\n" in item or "\r" in item:
                problems.append(f"task '{name}' argv[{index}] contains a control character")
            if item in _PLACEHOLDERS:
                continue
            if any(marker in item for marker in ("{", "}", "%", "$", "`")):
                problems.append(f"task '{name}' argv[{index}] looks like it was built from "
                                f"a value ('{item[:40]}')")
        if not isinstance(spec.get("read_only", True), bool):
            problems.append(f"task '{name}' read_only is not a bool")
        try:
            timeout = float(spec.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            problems.append(f"task '{name}' timeout is not a number")
            continue
        if not (0 < timeout <= MAX_TIMEOUT):
            problems.append(f"task '{name}' timeout {timeout} is outside "
                            f"0..{MAX_TIMEOUT:.0f}s")
        needs = spec.get("needs") or {}
        if not isinstance(needs, dict):
            problems.append(f"task '{name}' needs is not a mapping")
            continue
        unknown = set(needs) - {"files", "exes", "modules"}
        if unknown:
            problems.append(f"task '{name}' needs unknown requirement kind(s): "
                            f"{sorted(unknown)}")
    return problems


def known_tasks(registry: Optional[Dict[str, Any]] = None) -> List[str]:
    """The names this node will answer to, sorted."""
    registry = NAMED_TASKS if registry is None else registry
    return sorted(str(name) for name in registry)


def resolve_task(name: Any,
                 registry: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The spec for ``name``, or None. Names are exact: no folding, no aliases."""
    registry = NAMED_TASKS if registry is None else registry
    if not isinstance(name, str):
        return None
    stripped = name.strip()
    if not stripped:
        return None
    spec = registry.get(stripped)
    return dict(spec) if isinstance(spec, dict) else None


def missing_requirements(spec: Dict[str, Any], repo_root: Optional[Path] = None,
                         which: Optional[Callable[[str], Optional[str]]] = None,
                         find_module: Optional[Callable[[str], Any]] = None) -> List[str]:
    """Why this node cannot run the task yet, as human reasons (empty means it can).

    A node is asked to run a *fixed* task, so every reason here is a property of the node:
    the script is not in this checkout, git is not installed, pytest is not importable. That
    is what a refusal should say -- "the peer did not answer" would hide it.
    """
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    which = which or shutil.which
    if find_module is None:
        import importlib.util as _ilu

        def find_module(module_name: str) -> Any:
            try:
                return _ilu.find_spec(module_name)
            except Exception:
                return None

    needs = spec.get("needs") or {}
    reasons: List[str] = []
    for relative in needs.get("files") or []:
        if not (root / str(relative)).exists():
            reasons.append(f"'{relative}' is not in this checkout")
    for exe in needs.get("exes") or []:
        if not which(str(exe)):
            reasons.append(f"'{exe}' is not installed")
    for module in needs.get("modules") or []:
        if find_module(str(module)) is None:
            reasons.append(f"the '{module}' module is not importable")
    return reasons


class SubprocessRunner:
    """The only slice of the machine a task needs.

    ``shell=False`` and a vector on purpose, the same as ``SubprocessAdbRunner``: nothing in
    the argv can become a command, because there is no command line -- there is a program and
    its arguments, and both come from the registry.
    """

    def __init__(self, python: Optional[str] = None):
        self.python = python or sys.executable or "python3"

    def argv_for(self, argv: Sequence[str]) -> List[str]:
        """The registry's argv with our own interpreter substituted in."""
        return [self.python if str(item) == PY else str(item) for item in argv]

    def run(self, argv: Sequence[str], cwd: str, timeout: float) -> Tuple[int, str, str]:
        """Run ``argv`` in ``cwd``; return ``(returncode, stdout, stderr)``."""
        vector = [str(item) for item in argv]
        try:
            proc = subprocess.run(  # noqa: S603 - vector args, no shell, registry-owned
                vector, cwd=(cwd or None), capture_output=True, text=True,
                timeout=max(0.1, float(timeout)), shell=False, check=False)
        except FileNotFoundError as exc:
            return EXIT_NOT_FOUND, "", f"{vector[0]} is not installed ({exc})"
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout if isinstance(exc.stdout, str) else ""
            return EXIT_TIMEOUT, partial, f"timed out after {float(timeout):.0f}s"
        except Exception as exc:                # pragma: no cover - exotic spawn failure
            return EXIT_NO_RUNNER, "", f"{type(exc).__name__}: {exc}"
        return (int(proc.returncode or 0), proc.stdout or "", proc.stderr or "")


def _clip(value: Any, limit: int = MAX_DETAIL) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def run_named_task(name: Any, *, runner: Optional[SubprocessRunner] = None,
                   audit: Optional[Any] = None, repo_root: Optional[Path] = None,
                   requested_by: str = "", registry: Optional[Dict[str, Any]] = None,
                   which: Optional[Callable[[str], Optional[str]]] = None,
                   find_module: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Run the task ``name`` on *this* node. The peer's half of ``dev_task``.

    ``name`` is the entire request. There is deliberately no argument for an argv, a cwd, an
    environment or an interpreter, which is what makes "the hub names it, the node enforces
    it" a property of the code rather than an aspiration.
    """
    registry = NAMED_TASKS if registry is None else registry
    task_name = str(name).strip() if isinstance(name, str) else ""
    spec = resolve_task(name, registry)
    if spec is None:
        return _refusal(f"unknown task '{task_name}'; this node knows "
                        f"{', '.join(known_tasks(registry))}", task_name,
                        requested_by=requested_by, audit=audit, registry=registry)
    missing = missing_requirements(spec, repo_root=repo_root, which=which,
                                   find_module=find_module)
    if missing:
        return _refusal(f"task '{task_name}' is not available on this node: "
                        + "; ".join(missing), task_name, requested_by=requested_by,
                        audit=audit, registry=registry)
    runner = runner or SubprocessRunner()
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    cwd = str(root / str(spec.get("cwd") or "."))
    timeout = float(spec.get("timeout", DEFAULT_TIMEOUT))
    argv = runner.argv_for(spec["argv"])
    read_only = bool(spec.get("read_only", True))
    _audit(audit, "dev_task_started", {
        "task": task_name, "argv": argv, "cwd": cwd, "timeout": timeout,
        "read_only": read_only, "requested_by": requested_by})
    started = time.time()
    exit_code, out, err = runner.run(argv, cwd, timeout)
    seconds = max(0.0, time.time() - started)
    ok = exit_code == 0
    detail = _clip(err if (not ok and err.strip()) else out) or _clip(out or err)
    _audit(audit, "dev_task_finished", {
        "task": task_name, "exit": exit_code, "ok": ok,
        "seconds": round(seconds, 2), "detail": detail})
    logger.info("dev_task %s requested by %s: exit=%s ok=%s in %.2fs argv=%s",
                task_name, requested_by or "unknown", exit_code, ok, seconds, argv)
    return {"status": "success" if ok else "error", "task": task_name, "ok": ok,
            "exit": exit_code, "seconds": round(seconds, 2), "argv": argv, "cwd": cwd,
            "timeout": timeout, "read_only": read_only, "detail": detail,
            # The delegation reply carries `delivered`: for a task it means "ran and
            # finished", which is what the hub reports as the outcome.
            "delivered": ok,
            # And a one-line summary, so the hub's reply says what the task actually reported
            # instead of only whether it exited cleanly.
            "message": detail,
            # What the wire actually carried, so the hub never trusts a claim about it.
            "sent": {"action_type": DELEGATE_ACTION, "params": {"task": task_name}}}


def _refusal(reason: str, task_name: str, *, requested_by: str = "",
             audit: Optional[Any] = None,
             registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    _audit(audit, "dev_task_refused", {"task": task_name, "reason": reason,
                                       "requested_by": requested_by})
    return {"status": "refused", "task": task_name, "reason": reason,
            "known_tasks": known_tasks(registry)}


def _audit(audit: Optional[Any], event_type: str, payload: Dict[str, Any]) -> None:
    """Append to the node's audit chain, best effort: audit never breaks a task."""
    if audit is None:
        return
    try:
        audit.append(event_type, payload)
    except Exception as exc:
        logger.warning("audit append failed for '%s': %s", event_type, exc)


class FleetDevTaskHandler:
    """Hub side: name a task, choose the peer, hand the name to the mesh.

    Nothing here builds a command. The handler's input is a task *name* and an optional peer
    id; every other key in the request is refused by name, because the request is the only
    thing a model or a console gets to write and it must not be able to write an argv.
    """

    def __init__(self, agent: Any, *, allowed_peers: Optional[Sequence[str]] = None,
                 audit: Optional[Any] = None,
                 registry: Optional[Dict[str, Any]] = None):
        self.agent = agent
        self.allowed_peers = {str(name).strip() for name in (allowed_peers or ())
                              if str(name).strip()}
        self.audit = audit
        self.registry = registry if registry is not None else NAMED_TASKS

    def handle(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        params = decision.get("params") if isinstance(decision, dict) else None
        return self.delegate(params if isinstance(params, dict) else {})

    def delegate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        smuggled = [key for key in FORBIDDEN_PARAM_KEYS if key in params]
        if smuggled:
            return self._refuse(f"a dev task request carries a task name, not "
                                f"{', '.join(sorted(smuggled))}")
        task = str(params.get("task") or "").strip()
        if not task:
            return self._refuse("params.task is required")
        if resolve_task(task, self.registry) is None:
            return self._refuse(f"unknown task '{task}'; the hive knows "
                                f"{', '.join(known_tasks(self.registry))}")
        peer, why = self._pick_peer(params.get("peer"))
        if peer is None:
            return self._refuse(why)
        payload = {"action_type": DELEGATE_ACTION, "params": {"task": task}}
        _audit(self.audit, "dev_task_requested", {
            "task": task, "peer": peer, "why": why, "payload": payload})
        delegate = getattr(self.agent, "_mesh_delegate", None)
        if not callable(delegate):
            return self._refuse("no delegation channel on this node")
        try:
            sent = dict(delegate(peer, payload) or {})
        except Exception as exc:
            sent = {"status": "error", "message": type(exc).__name__}
        self._log("dev task named: %s -> %s (%s)", task, peer, why)
        return {"status": str(sent.get("status") or "error"), "action": DEV_TASK_ACTION,
                "task": task, "peer": peer, "route": why, "wire": payload, "mesh": sent}

    def _pick_peer(self, wanted: Any) -> Tuple[Optional[str], str]:
        """Which node runs it: the one named, or the fleet's best-ranked live peer.

        Ranked by the election's own priority number (the hub is 1, a Mac 10, a phone 500),
        so "the least loaded node that can do the work" is the same ordering the lease uses.
        """
        name = str(wanted or "").strip()
        if name:
            if self.allowed_peers and name not in self.allowed_peers:
                return None, f"peer '{name}' is not in the operator allowlist"
            return name, "named by the request"
        peers = self._live_peers()
        if not peers:
            return None, ("no peer is known to this node right now -- a quiet mesh and an "
                          "empty fleet look the same from here")
        peers.sort(key=lambda entry: (entry.get("priority", 999),
                                      str(entry.get("device_id") or "")))
        best = peers[0]
        return str(best.get("device_id") or ""), (
            f"lowest priority number ({best.get('priority')}) among {len(peers)} peer(s)")

    def _live_peers(self) -> List[Dict[str, Any]]:
        telemetry = getattr(self.agent, "telemetry", None)
        peers = (telemetry or {}).get("mesh_peers") if isinstance(telemetry, dict) else None
        own = str(getattr(self.agent, "node_id", "") or "")
        return [dict(entry) for entry in (peers or [])
                if isinstance(entry, dict)
                and str(entry.get("device_id") or entry.get("node_id") or "") != own]

    def _refuse(self, reason: str) -> Dict[str, Any]:
        _audit(self.audit, "dev_task_refused", {"reason": reason})
        self._log("dev task refused: %s", reason)
        return {"status": "refused", "action": DEV_TASK_ACTION, "reason": reason}

    def _log(self, message: str, *args: Any) -> None:
        logger.info(message, *args)
        log = getattr(self.agent, "log", None)
        if callable(log):
            try:
                log("FLEET", message % args if args else message)
            except Exception:
                pass


def register_dev_task_handlers(execution_layer: Any, handler: FleetDevTaskHandler,
                               policy_module: Optional[Any] = None) -> List[str]:
    """Register the hub's dev-task handler and adopt the action type into the vocabulary.

    Mirrors ``fleet_deploy.register_fleet_handlers``: the execution layer gets the handler and
    ``policy`` learns the name, so a node with this module can both propose and perform a dev
    task, and a node without it never can. The gates are not loosened here -- the engine
    consent-gates ``policy.FLEET_ACTION_TYPES`` and the approval broker adds its human gate,
    and this function only makes the name known *to* those gates.
    """
    policy = policy_module
    if policy is None:
        try:
            import policy as _policy
            policy = _policy
        except Exception:                   # pragma: no cover - policy ships
            policy = None
    if policy is not None:
        for attribute in ("FLEET_ACTION_TYPES", "KNOWN_ACTION_TYPES"):
            current = getattr(policy, attribute, None)
            if current is None:
                setattr(policy, attribute, {DEV_TASK_ACTION})
                continue
            try:
                current.add(DEV_TASK_ACTION)
            except AttributeError:          # frozenset / exotic container
                setattr(policy, attribute, set(current) | {DEV_TASK_ACTION})
    execution_layer.register_handler(DEV_TASK_ACTION, handler.handle)
    logger.info("registered the fleet dev-task handler ('%s')", DEV_TASK_ACTION)
    return [DEV_TASK_ACTION]
