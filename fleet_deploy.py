"""Fleet deployment over ADB: roll a signed build onto allowlisted devices.

Why this exists
---------------
The hive was updated by hand: build an APK, push it with ``adb install``, and
hope the node comes back with its memory intact. That works, but it is the one
operation that can silently take a node off the fleet -- a build signed by a
key the device does not trust fails with ``INSTALL_FAILED_UPDATE_INCOMPATIBLE``
and the only way forward is an uninstall, which wipes the memory DB and every
cached model (3.25 GB of GGUFs on the Tab S9 FE). It was also invisible to the
governance layer: no consent, no approval, no audit.

This module makes it an ordinary, gated capability instead:

- the vocabulary lives in :mod:`policy` (``FLEET_ACTION_TYPES`` =
  ``{"fleet_deploy"}``, ``FLEET_READ_ACTION_TYPES`` = ``{"fleet_status"}``);
- the decision engine consent-gates ``fleet_deploy`` (an operator grant must
  exist before a rollout can execute) and the approval broker adds its own
  human gate on top;
- every attempt is appended to the audit chain, per target;
- the handler refuses anything that is not an APK inside the operator's
  artifact root, refuses targets outside the operator's serial allowlist, and
  refuses an artifact whose SHA-256 does not match the expected digest.

The transport is the wireless ADB link the fleet already uses (``adb pair`` /
``adb connect``), so no device-side code is involved. ``FleetDeployHandler``
takes its ADB surface as a dependency (:class:`AdbRunner`), which keeps the
whole capability unit-testable without a device and without spawning a single
process.

Host-only by design: this module is not part of the Android bundle, so the
import probe in ``decision_engine`` leaves the fleet action types unknown on a
phone -- a device can never propose a deploy it has no means to perform.
"""
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from policy import FLEET_ACTION_TYPES, FLEET_READ_ACTION_TYPES

logger = logging.getLogger(__name__)

DEFAULT_PACKAGE = "com.samurai.shugocore"
# A rollout is bounded: the operator allowlist is the real gate, this keeps a
# runaway (or hallucinated) target list from stampeding the mesh.
MAX_TARGETS = 8
DEFAULT_TIMEOUT = 240.0
ARTIFACT_SUFFIXES = (".apk",)
_DETAIL_LIMIT = 300


class AdbRunner(Protocol):
    """The only slice of ``adb`` the handler needs (injectable for tests)."""

    def run(self, args: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        """Run ``adb <args>``; return ``(returncode, stdout, stderr)``."""
        ...


class SubprocessAdbRunner:
    """``AdbRunner`` backed by the real adb binary.

    No shell is involved (``shell=False``), so a serial or path can never turn
    into a command: the arguments are passed as a vector and every one of them
    is validated by the handler first.
    """

    def __init__(self, adb_path: Optional[str] = None):
        self.adb_path = adb_path or shutil.which("adb") or "adb"

    def available(self) -> bool:
        """True when the resolved adb is runnable (PATH hit or explicit file)."""
        return bool(shutil.which(self.adb_path)) or os.path.isfile(self.adb_path)

    def run(self, args: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(  # noqa: S603 - vector args, no shell
                [self.adb_path, *args],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except FileNotFoundError as exc:
            return 127, "", f"adb not found ({self.adb_path}): {exc}"
        except subprocess.TimeoutExpired:
            return 124, "", f"adb timed out after {timeout:.0f}s"
        except OSError as exc:
            return 126, "", f"adb could not start: {exc}"


def _clip(text: Any, limit: int = _DETAIL_LIMIT) -> str:
    """One bounded, single-line rendering of a tool result for the audit chain."""
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 (an APK is ~75 MB: never read it whole twice)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_devices(output: str) -> List[Tuple[str, str]]:
    """Parse ``adb devices -l`` into ``[(serial, state), ...]``.

    ``-l`` appends transport details after the state, e.g. ``adb-R52WC05…_
    adb-tls-connect._tcp   device product:gts9fesqw model:SM_X518U``, so only
    the first two whitespace-separated tokens matter. The
    ``List of devices attached`` header and blank lines are skipped.
    """
    found: List[Tuple[str, str]] = []
    for line in str(output or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("List"):
            continue
        if parts[1] not in ("device", "offline", "unauthorized"):
            continue
        found.append((parts[0], parts[1]))
    return found


class FleetDeployHandler:
    """Execution-layer handler for ``fleet_deploy`` / ``fleet_status``.

    Fail-closed on every axis: no operator allowlist, an artifact outside the
    artifact root, a digest mismatch, an unknown target or too many targets all
    refuse without touching a device.
    """

    def __init__(self, adb: AdbRunner,
                 allowed_targets: Iterable[str] = (),
                 package: str = DEFAULT_PACKAGE,
                 artifact_root: Optional[str] = None,
                 audit: Optional[Any] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_targets: int = MAX_TARGETS):
        self.adb = adb
        self.allowed_targets = {str(t).strip() for t in allowed_targets
                                if str(t).strip()}
        self.package = str(package or DEFAULT_PACKAGE)
        self.artifact_root = (Path(str(artifact_root)).expanduser().resolve()
                              if artifact_root else None)
        self.audit = audit
        self.timeout = max(5.0, float(timeout))
        self.max_targets = max(1, int(max_targets))

    # -- execution-layer entry point -------------------------------------

    def handle(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        action_type = str(decision.get("action_type", ""))
        params = decision.get("params") or {}
        if action_type == "fleet_status":
            return self.status()
        if action_type == "fleet_deploy":
            return self.deploy(params)
        return {"status": "refused",
                "reason": f"unknown fleet action '{action_type}'"}

    # -- read-only -------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Attached devices, and the build each one currently runs."""
        rc, out, err = self.adb.run(["devices", "-l"], self.timeout)
        if rc != 0:
            return {"status": "failed", "action": "fleet_status",
                    "reason": _clip(err or out or f"adb exited {rc}")}
        devices: List[Dict[str, Any]] = []
        for serial, state in _parse_devices(out):
            entry: Dict[str, Any] = {
                "serial": serial,
                "state": state,
                "allowlisted": serial in self.allowed_targets,
            }
            if state == "device":
                entry["installed"] = self._installed(serial)
            devices.append(entry)
        return {"status": "success", "action": "fleet_status",
                "devices": devices, "allowlist": sorted(self.allowed_targets)}

    def _installed(self, serial: str) -> Dict[str, Any]:
        """Installed versionName / versionCode for the package, when present."""
        rc, out, err = self.adb.run(
            ["-s", serial, "shell", "dumpsys", "package", self.package],
            self.timeout)
        if rc != 0:
            return {"error": _clip(err or out or f"adb exited {rc}")}
        info: Dict[str, Any] = {}
        for line in str(out or "").splitlines():
            token = line.strip()
            if token.startswith(("versionName=", "versionCode=")):
                key, _, value = token.partition("=")
                # 'versionCode=22 minSdk=28 targetSdk=34' -> keep the first.
                parts = value.split()
                info[key] = parts[0] if parts else ""
        return info

    # -- side-effecting --------------------------------------------------

    def deploy(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Install ``params['artifact']`` onto the allowlisted targets."""
        artifact, digest, reason = self._resolve_artifact(params)
        if artifact is None:
            return self._refuse(reason)
        targets, reason = self._resolve_targets(params)
        if targets is None:
            return self._refuse(reason)

        dry_run = bool(params.get("dry_run"))
        expect_version = str(params.get("expect_version") or "").strip()
        results = [self._deploy_one(serial, artifact, dry_run, expect_version)
                   for serial in targets]
        deployed = sum(1 for entry in results if entry.get("ok"))
        failed = len(results) - deployed
        if not deployed:
            status = "failed"
        elif failed:
            status = "partial"
        else:
            status = "success"
        payload = {
            "status": status,
            "action": "fleet_deploy",
            "artifact": artifact.name,
            "sha256": digest,
            "dry_run": dry_run,
            "deployed": deployed,
            "failed": failed,
            "results": results,
        }
        self._audit("fleet_deploy", {
            "status": status, "artifact": artifact.name, "sha256": digest,
            "targets": targets, "deployed": deployed, "failed": failed,
            "dry_run": dry_run,
        })
        return payload

    def _resolve_artifact(
            self, params: Dict[str, Any]) -> Tuple[Optional[Path], str, str]:
        """``(path, sha256, refusal-reason)`` for the requested artifact."""
        raw = str(params.get("artifact") or "").strip()
        if not raw:
            return None, "", "artifact path is required"
        path = Path(raw).expanduser()
        try:
            path = path.resolve()
        except OSError as exc:
            return None, "", f"artifact path is unusable: {_clip(exc)}"
        if not path.is_file():
            return None, "", f"artifact '{path.name}' is not a file"
        if path.suffix.lower() not in ARTIFACT_SUFFIXES:
            return None, "", (f"artifact '{path.name}' is not "
                              f"{'/'.join(ARTIFACT_SUFFIXES)}")
        if self.artifact_root is not None:
            try:
                path.relative_to(self.artifact_root)
            except ValueError:
                return None, "", (f"artifact '{path.name}' is outside the "
                                  f"operator artifact root")
        digest = sha256_file(path)
        expected = str(params.get("sha256") or "").strip().lower()
        if expected and expected != digest:
            return None, "", (f"artifact sha256 {digest[:12]} does not match "
                              f"the expected {expected[:12]}")
        return path, digest, ""

    def _resolve_targets(
            self, params: Dict[str, Any]) -> Tuple[Optional[List[str]], str]:
        """``(targets, refusal-reason)``: allowlisted, bounded, de-duplicated."""
        if not self.allowed_targets:
            return None, ("no deployment targets are allowlisted for this node "
                          "(the operator must allow the serials first)")
        requested = params.get("targets")
        if isinstance(requested, str):
            requested = [requested]
        targets = [str(t).strip() for t in (requested or [])
                   if str(t).strip()]
        if not targets:
            targets = sorted(self.allowed_targets)
        if len(targets) > self.max_targets:
            return None, (f"{len(targets)} targets exceed the "
                          f"{self.max_targets}-device rollout bound")
        unknown = [t for t in targets if t not in self.allowed_targets]
        if unknown:
            return None, f"target(s) not in the operator allowlist: {unknown}"
        return sorted(set(targets)), ""

    def _deploy_one(self, serial: str, artifact: Path, dry_run: bool,
                    expect_version: str) -> Dict[str, Any]:
        if dry_run:
            entry: Dict[str, Any] = {"serial": serial, "ok": True,
                                     "detail": "dry run: install skipped"}
        else:
            rc, out, err = self.adb.run(
                ["-s", serial, "install", "-r", str(artifact)], self.timeout)
            entry = {
                "serial": serial,
                "ok": rc == 0 and "Success" in str(out or ""),
                "detail": _clip(f"{out}\n{err}"),
            }
            if entry["ok"] and expect_version:
                installed = self._installed(serial)
                entry["installed"] = installed
                seen = str(installed.get("versionName") or "")
                if seen != expect_version:
                    entry["ok"] = False
                    entry["detail"] = (f"installed versionName '{seen}' does "
                                       f"not match expected "
                                       f"'{expect_version}'")
        self._audit("fleet_deploy_target", {"serial": serial, "ok": entry["ok"],
                                            "artifact": artifact.name,
                                            "detail": entry["detail"]})
        return entry

    def _refuse(self, reason: str) -> Dict[str, Any]:
        self._audit("fleet_deploy_refused", {"reason": reason})
        return {"status": "refused", "action": "fleet_deploy", "reason": reason}

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception as exc:            # audit must never break a rollout
            logger.warning("audit append failed for '%s': %s", event_type, exc)


def register_fleet_handlers(execution_layer: Any,
                            handler: "FleetDeployHandler",
                            policy_module: Optional[Any] = None) -> None:
    """Register the fleet action types and adopt them into the vocabulary.

    Mirrors ``shugonet_bridge.register_network_handlers``: the execution layer
    gets one handler per action type, and ``policy.KNOWN_ACTION_TYPES`` is
    extended so any module validating against it accepts the new names.
    """
    types = sorted(FLEET_ACTION_TYPES | FLEET_READ_ACTION_TYPES)
    policy = policy_module
    if policy is None:
        try:
            import policy as _policy
            policy = _policy
        except Exception:                   # pragma: no cover - policy ships
            policy = None
    if policy is not None:
        if not hasattr(policy, "KNOWN_ACTION_TYPES"):
            setattr(policy, "KNOWN_ACTION_TYPES", set())
        policy.KNOWN_ACTION_TYPES.update(types)

    for action_type in types:
        execution_layer.register_handler(action_type, handler.handle)

    logger.info("registered %d fleet action handlers", len(types))

