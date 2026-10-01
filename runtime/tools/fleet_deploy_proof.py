"""Live proof: drive fleet_deploy against the real phones with real adb.

    python runtime/tools/fleet_deploy_proof.py [--install]

Without ``--install`` it only runs the read-only ``fleet_status`` and a
``dry_run`` deployment plan. With ``--install`` it also performs the real
in-place upgrade on the allowlisted serials -- the point being that a build
signed with the fleet key upgrades a device *without* the uninstall that would
wipe its memory and models.

Audit events go to a dedicated chain file (never the running desktop agent's
chain, which is hash-linked and single-writer).
"""
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fleet_deploy import (  # noqa: E402
    FleetDeployHandler,
    SubprocessAdbRunner,
    sha256_file,
)

ADB = r"G:\Android\Sdk\platform-tools\adb.exe"
APK = (r"C:\Users\marku\StudioProjects\ShugoCore\platforms\android\app"
       r"\build\outputs\apk\debug\app-debug.apk")
TARGETS = [
    "adb-R52WC05JPMW-4kMS88._adb-tls-connect._tcp",   # Tab S9 FE
    "adb-R58N92Q0XRK-BM3bmS._adb-tls-connect._tcp",   # A51
    "adb-RZCY71M7CZW-TzE1tS._adb-tls-connect._tcp",   # A16 (third node)
]
AUDIT_PATH = REPO_ROOT / "runtime" / "device_backups" / "fleet_deploy_audit.jsonl"


class ChainShim:
    """Minimal stand-in for the audit chain (append-only JSONL, hash-free)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type, payload):
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "type": event_type,
                 "payload": payload}
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def main(argv):
    install = "--install" in argv
    artifact_root = str(Path(APK).parent)
    handler = FleetDeployHandler(adb=SubprocessAdbRunner(ADB),
                                 allowed_targets=TARGETS,
                                 artifact_root=artifact_root,
                                 audit=ChainShim(AUDIT_PATH))

    print("=== fleet_status ===")
    print(json.dumps(handler.status(), indent=2)[:1600])

    print("\n=== fleet_deploy (dry run) ===")
    print(json.dumps(handler.deploy({"artifact": APK, "dry_run": True}),
                     indent=2)[:900])

    print("\n=== refusal probes (must not touch a device) ===")
    for params, label in (
        ({"artifact": APK, "targets": ["not-an-allowlisted-serial"]},
         "unknown target"),
        ({"artifact": APK, "sha256": "00" * 32}, "bad digest"),
        ({"artifact": str(Path(APK).with_name("nope.apk"))}, "missing artifact"),
    ):
        result = handler.deploy(params)
        print(f"  {label}: {result['status']} - {result.get('reason', '')[:80]}")

    if install:
        digest = sha256_file(Path(APK))
        print(f"\n=== fleet_deploy (REAL, sha256 {digest[:16]}...) ===")
        result = handler.deploy({"artifact": APK, "sha256": digest,
                                 "expect_version": "1.30.22"})
        print(json.dumps(result, indent=2)[:2000])

    print("\n=== audit chain written ===")
    if AUDIT_PATH.exists():
        for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-6:]:
            print("  " + line[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
