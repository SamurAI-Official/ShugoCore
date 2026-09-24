#!/usr/bin/env python3
"""Integrate the ShugoCore XR addon with a host Godot XR project.

Strategy (Phase 4): the host project (already Quest-deployable) gains the
ShugoCore addon by SYMLINK — repo stays the single source of truth, while
the host keeps its proven export preset, Gradle shell, vendors plugin.

Sub-tasks, each idempotent:
  1. symlink addon + scripts + console + scene + default JSON
  2. register the ShugoCoreBridge autoload in host project.godot
  3. flip permissions/internet=true in the host's "Meta Quest" preset
  4. verify routes cross-check via ShugoCoreServer.route_name

Usage: python3 integrate_sample.py [--host PATH] [--apply]
Dry run by default; --apply performs. Step 2 is refused while a Godot
editor holds the host project open (project.godot races the editor).
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
GODOT_DIR = REPO / "platforms" / "godot"
DEFAULT_HOST = Path("/Users/0exe/body-tracking-xr-sample")

ADDON_LINKS = [
    ("addons/shugocore_xr", "addons/shugocore_xr"),
    ("scripts/shugo_presence.gd", "scripts/shugo_presence.gd"),
    ("scripts/shugocore_agent_bridge.gd", "scripts/shugocore_agent_bridge.gd"),
    ("scripts/shugocore_config.gd", "scripts/shugocore_config.gd"),
    ("scripts/xr_bootstrap.gd", "scripts/xr_bootstrap.gd"),
    ("scripts/console", "scripts/console"),
    ("scenes/operator_console.tscn", "scenes/operator_console.tscn"),
    ("shugocore_xr.default.json", "shugocore_xr.default.json"),
]

AUTOLOAD_LINE = 'ShugoCoreBridge="*res://scripts/shugocore_agent_bridge.gd"'

ROUTES = [
    ("GET", "/health"),
    ("GET", "/api/v1/status"),
    ("GET", "/api/v1/sensors"),
    ("POST", "/api/generate"),
    ("POST", "/api/v1/task"),
    ("GET", "/api/v1/fleet"),
    ("GET", "/api/v1/approvals"),
]


def editor_holds(path: Path) -> bool:
    """Best-effort: is a Godot editor running --path <host>?"""
    try:
        out = subprocess.run(["pgrep", "-af", "Godot"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return False
    return any(str(path) in line and "--editor" in line
               for line in out.splitlines())


def main() -> int:
    host = Path(sys.argv[sys.argv.index("--host") + 1]) \
        if "--host" in sys.argv else DEFAULT_HOST
    apply = "--apply" in sys.argv
    print(f"host={host} apply={apply}")
    if not host.exists():
        print("host project not found")
        return 1

    plan = []
    for src_rel, dst_rel in ADDON_LINKS:
        src = GODOT_DIR / src_rel
        dst = host / dst_rel
        if not src.exists():
            print(f"REPO SOURCE MISSING: {src_rel} (aborting)")
            return 1
        if dst.is_symlink() and dst.resolve() == src.resolve():
            plan.append(("ok", f"symlink present: {dst_rel}"))
        elif dst.exists() or dst.is_symlink():
            plan.append(("conflict", f"{dst_rel} exists (not our symlink)"))
        else:
            plan.append(("link", f"{dst_rel} -> {src_rel}"))

    proj = host / "project.godot"
    ptext = proj.read_text()
    if AUTOLOAD_LINE in ptext:
        plan.append(("ok", "autoload ShugoCoreBridge registered"))
    else:
        plan.append(("autoload", "register ShugoCoreBridge in project.godot"))

    preset = host / "export_presets.cfg"
    prtext = preset.read_text()
    if "permissions/internet=true" in prtext:
        plan.append(("ok", "internet permission already on"))
    else:
        q0 = prtext.index('name="Meta Quest"')
        q1 = prtext.index("[preset.1]", q0)
        if "permissions/internet=false" in prtext[q0:q1]:
            plan.append(("preset", "flip internet=false->true "
                                   "in Meta Quest block"))
        else:
            plan.append(("manual", "internet key missing in Meta Quest block"))

    sys.path.insert(0, str(REPO))
    from shugocore_server import ShugoCoreServer
    missing = [p for m, p in ROUTES
               if ShugoCoreServer.route_name(m, p) is None]
    bridge = (GODOT_DIR / "scripts" / "shugocore_agent_bridge.gd").read_text()
    ungreeted = [p for _, p in ROUTES if f'"{p}"' not in bridge]
    plan.append(("ok" if not missing else "server",
                 f"{len(ROUTES) - len(missing)}/{len(ROUTES)} routes "
                 "resolve server-side"))
    plan.append(("ok" if not ungreeted else "bridge",
                 f"bridge greets: {ungreeted if ungreeted else 'all routes'}"))

    holds = editor_holds(host)
    for kind, msg in plan:
        print(f"  [{kind:8s}] {msg}")
    print(f"  [info    ] editor holds host: {holds}")

    needs_apply = [k for k, _ in plan
                   if k in ("link", "autoload", "preset")]
    if not needs_apply:
        print("nothing to apply")
        return 0
    if not apply:
        print("dry run — re-run with --apply")
        return 0
    if holds and "autoload" in needs_apply:
        print("refusing project.godot edit: editor holds host. "
              "Close it, or apply symlinks-only manually.")
        return 2
    for src_rel, dst_rel in ADDON_LINKS:
        dst = host / dst_rel
        if dst.is_symlink() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(GODOT_DIR / src_rel)
        print(f"linked {dst_rel}")
    if "autoload" in needs_apply:
        t = proj.read_text()
        t = t.replace("[autoload]\n",
                      "[autoload]\n" + AUTOLOAD_LINE + "\n", 1)
        proj.write_text(t)
        print("autoload registered")
    if "preset" in needs_apply:
        t = preset.read_text()
        q0 = t.index('name="Meta Quest"')
        q1 = t.index("[preset.1]", q0)
        block = t[q0:q1].replace("permissions/internet=false",
                                 "permissions/internet=true", 1)
        preset.write_text(t[:q0] + block + t[q1:])
        print("internet permission enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
