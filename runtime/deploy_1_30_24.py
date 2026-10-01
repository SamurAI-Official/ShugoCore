"""Deploy 1.30.23 to the attached phones through the real fleet_deploy path.

A bare ``adb install -r`` would work and skip everything this project built for it:
the handler refuses a target that is not allowlisted, a digest that does not match,
an artifact outside the artifact root, and it appends every attempt -- including every
refusal -- to an audit chain. This driver supplies those inputs from the devices that
are attached right now, so the upgrade is the gated one and the evidence lands in
``runtime/device_backups/``.

    python runtime/deploy_1_30_23.py            # plan only: status, dry run, probes
    python runtime/deploy_1_30_23.py --install  # the real in-place upgrade

The upgrade is in place, which is the whole point: an uninstall would take the
device's memory and models with it, and the capability baseline is what proves
afterwards that nothing was lost.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fleet_deploy import (  # noqa: E402
    FleetDeployHandler,
    SubprocessAdbRunner,
    sha256_file,
)

ADB = r"G:\Android\Sdk\platform-tools\adb.exe"
APK = (REPO_ROOT / "platforms" / "android" / "app" / "build" / "outputs"
       / "apk" / "debug" / "app-debug.apk")
EXPECT_VERSION = "1.30.24"
PACKAGE = "com.samurai.shugocore"
AUDIT_PATH = REPO_ROOT / "runtime" / "device_backups" / "fleet_deploy_audit.jsonl"


class ChainShim:
    """Append-only JSONL stand-in for the audit chain (never the agent's own)."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type, payload):
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "type": event_type,
                 "payload": payload}
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def attached():
    """The devices adb sees right now, as (serial, model) pairs."""
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                         timeout=30).stdout
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            model = subprocess.run([ADB, "-s", parts[0], "shell", "getprop",
                                    "ro.product.model"],
                                   capture_output=True, text=True,
                                   timeout=30).stdout.strip() or "unknown"
            found.append((parts[0], model))
    return found


def device_version(serial):
    out = subprocess.run([ADB, "-s", serial, "shell", "dumpsys", "package",
                          PACKAGE], capture_output=True, text=True,
                         timeout=60).stdout
    for line in out.splitlines():
        if "versionName=" in line:
            return line.strip().split("versionName=")[-1].split()[0]
    return "not installed"


# The provider logs under its own tag, which is why a filter that only knows the service and
# bridge silently returns nothing -- the same empty-output trap this feature exists to avoid.
EVIDENCE_TAGS = ("ShugoCoreService", "SoundBridge", "SoundProvider")
SOUND_EVIDENCE_TOKENS = ("sound self-test", "Sound runtime ready", "listening for sound",
                         "not listening:", "perception mode:", "sound: policy says")


def sound_evidence(serial, lines=400):
    """The sound runtime's own account of this launch, straight from logcat.

    Tag-filtered at the source: on a Samsung build the unfiltered buffer is drowned in
    audio-resampler noise, so a line-count window can miss the very lines being looked for.
    The service self-tests the native path before any Python call, so these lines exist on
    every start: they either show both models loading and a zero window coming back labelled
    silence by the classifier itself, or they say why not. Nothing is inferred from the build.
    """
    # No -t here on purpose: logcat applies its tail limit to the raw buffer *before* the tag
    # filter, so "-t 400 -s SoundProvider:*" returns nothing whenever the most recent 400 lines
    # come from other processes -- which on a Samsung build is most of the time. That is why
    # the first evidence runs looked empty rather than wrong. The filtered stream is small
    # enough to read whole. ("lines" is accepted for callers that still pass it.)
    args = [ADB, "-s", serial, "logcat", "-d"]
    for tag in EVIDENCE_TAGS:
        args += ["-s", tag + ":*"]
    out = subprocess.run(args, capture_output=True, text=True, timeout=180).stdout
    found = []
    for line in out.splitlines():
        if any(token.lower() in line.lower() for token in SOUND_EVIDENCE_TOKENS):
            found.append(line.strip()[:200])
    return found


def wait_for_sound_evidence(serial, timeout=75, lines=1200):
    """Wait for the sound runtime's lines to appear, then return them.

    A service init loads the local GGUF *and* both ONNX models, so these lines can take tens
    of seconds to appear. Sampling once right after launch is how you get an empty result and
    then mistake it for silence -- which is exactly the failure this whole feature exists to
    avoid, so the driver waits instead.
    """
    deadline = time.time() + timeout
    found = []
    while time.time() < deadline:
        found = sound_evidence(serial, lines)
        if found:
            return found
        time.sleep(5)
    return found


def relaunch_and_report(serial):
    """An install stops the app, so the node is not back until it is started."""
    subprocess.run([ADB, "-s", serial, "shell", "monkey", "-p", PACKAGE,
                    "-c", "android.intent.category.LAUNCHER", "1"],
                   capture_output=True, text=True, timeout=60)
    time.sleep(6)
    return device_version(serial)


def main(argv):
    install = "--install" in argv
    if not APK.is_file():
        print(f"no APK at {APK} -- build it first")
        return 1
    devices = attached()
    if not devices:
        print("no devices attached")
        return 1
    print("attached: " + ", ".join(f"{model} ({serial[-12:]})"
                                   for serial, model in devices))
    for serial, model in devices:
        print(f"  {model} currently runs {device_version(serial)}")

    handler = FleetDeployHandler(adb=SubprocessAdbRunner(ADB),
                                 allowed_targets=[serial for serial, _ in devices],
                                 artifact_root=str(APK.parent),
                                 audit=ChainShim(AUDIT_PATH))
    print("\n=== fleet_status ===")
    print(json.dumps(handler.status(), indent=2)[:1400])

    print("\n=== fleet_deploy (dry run) ===")
    print(json.dumps(handler.deploy({"artifact": str(APK), "dry_run": True}),
                     indent=2)[:900])

    print("\n=== refusal probes (must not touch a device) ===")
    for params, label in (({"artifact": str(APK), "targets": ["not-allowlisted"]},
                           "unknown target"),
                          ({"artifact": str(APK), "sha256": "00" * 32},
                           "bad digest")):
        result = handler.deploy(params)
        print(f"  {label}: {result.get('status')} - "
              f"{str(result.get('reason', ''))[:80]}")

    if not install:
        print("\n(dry run only -- pass --install to really upgrade)")
        return 0

    digest = sha256_file(APK)
    print(f"\n=== fleet_deploy (REAL, sha256 {digest[:16]}..., "
          f"expect {EXPECT_VERSION}) ===")
    result = handler.deploy({"artifact": str(APK), "sha256": digest,
                             "expect_version": EXPECT_VERSION})
    print(json.dumps(result, indent=2)[:2400])

    print("\n=== relaunch and report ===")
    for serial, model in devices:
        print(f"  {model}: now runs {relaunch_and_report(serial)}")

    print("\n=== sound evidence (on-device; nothing here is inferred) ===")
    for serial, model in devices:
        lines = wait_for_sound_evidence(serial)
        print(f"  {model}: {len(lines)} matching line(s)")
        for line in lines[-6:]:
            print("    " + line)
        if not lines:
            print("    (nothing after 75s: the runtime refused, or the service never started)")

    print("\n=== audit trail ===")
    if AUDIT_PATH.exists():
        for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-6:]:
            print("  " + line[:190])
    status = str(result.get("status", "")).lower()
    return 1 if status in ("refused", "failed", "error") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))