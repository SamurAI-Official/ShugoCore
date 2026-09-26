#!/usr/bin/env python3
"""Build llama.cpp for the mesh layer split (Option 2, docs/layer_split_rpc.md).

Two builds matter for distributed inference over the hive:

* **host** -- ``llama-server`` and ``ggml-rpc-server`` with ``GGML_RPC=ON``, so a
  host can assign layers to peripheral devices;
* **peripheral** -- the Android arm64 ``ggml-rpc-server`` that the app packages as
  ``lib/arm64-v8a/libshugocore_rpc_server.so`` (the APK build also produces it;
  this exists so the same commit can be built off-device for probes).

The commands used to live only in prose, which made every measurement depend on
someone's shell history. Every result is also a *version* result: the pinned
submodule is the source of truth, so its commit hash is recorded in the evidence
file this script writes.

    python scripts/build_llama_rpc.py --host                  # this machine
    python scripts/build_llama_rpc.py --android --ndk <dir>   # arm64 peripheral
    python scripts/build_llama_rpc.py --host --verify-api     # + source facts
"""
import argparse
import glob
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBMODULE = Path("platforms/android/app/src/main/cpp/llama.cpp")
DEFAULT_HOST_BUILD = Path("G:/Android/llama-rpc/host")
DEFAULT_ANDROID_BUILD = Path("G:/Android/llama-rpc/arm64")
EVIDENCE = Path("runtime/evidence/mesh_model_host/llama_build.txt")

# Targets that matter: the server hosts the model, the rpc server is the
# peripheral's offload endpoint. Examples and tests stay off to keep the build
# small enough to run before every measurement.
HOST_TARGETS = ("llama-server", "ggml-rpc-server")
ANDROID_TARGETS = ("ggml-rpc-server",)

# Deliberately off: LLAMA_CURL pulls a network dependency into a build whose
# whole point is local inference, and the mesh never fetches models by URL.
COMMON_DEFINES = {
    "GGML_RPC": "ON",
    "LLAMA_CURL": "OFF",
    "LLAMA_BUILD_TESTS": "OFF",
    "LLAMA_BUILD_EXAMPLES": "OFF",
    "CMAKE_BUILD_TYPE": "Release",
}


def parse_submodule_status(text: str, path: str = "") -> str:
    """Commit hash for ``path`` from ``git submodule status`` output.

    Lines look like `` 6c977e2... path (describe)`` or ``-6703d78... path``,
    where a leading ``-`` means the submodule is not checked out. Returns "" when
    the path is absent, and raises when it is present but uninitialised -- a build
    against an empty directory would otherwise fail deep inside CMake.
    """
    wanted = str(path).replace("\\", "/").strip("/")
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        marker = parts[0][0]
        commit = parts[0].lstrip("+-U")
        found = parts[1].replace("\\", "/").strip("/")
        if wanted and found != wanted:
            continue
        if marker == "-":
            raise RuntimeError(f"submodule not checked out: {found} (run "
                               f"'git submodule update --init {found}')")
        return commit
    return ""


def state_is_populated(path: Path, exists=os.path.exists) -> bool:
    """True when a submodule directory actually has content."""
    if not exists(str(path)):
        return False
    try:
        return any(True for _ in os.scandir(str(path)))
    except OSError:
        return False


def find_tool(names, explicit=None, extra_dirs=(), which=shutil.which,
              exists=os.path.exists):
    """Locate a build tool: explicit path, then known dirs, then PATH.

    The Android SDK ships CMake and Ninja that work fine as *host* tools, which
    is why its directories are searched before PATH.
    """
    if explicit:
        # A stale operator path must fall back to discovery, not become a
        # hard failure: the tool may have moved and the SDK copy is fine.
        if exists(str(explicit)):
            return str(explicit)
    for directory in extra_dirs:
        for name in names:
            candidate = os.path.join(str(directory), name)
            if exists(candidate):
                return candidate
    for name in names:
        found = which(name)
        if found:
            return found
    return None


def android_cmake_dirs(env=None, globber=glob.glob) -> list:
    """CMake/``bin`` directories inside an installed Android SDK."""
    env = os.environ if env is None else env
    roots = [env.get("ANDROID_HOME"), env.get("ANDROID_SDK_ROOT"),
             "G:/Android/Sdk"]
    dirs = []
    for root in roots:
        if not root:
            continue
        for hit in sorted(globber(os.path.join(root, "cmake", "*", "bin"))):
            if hit not in dirs:                 # ANDROID_HOME often repeats the
                dirs.append(hit)                # fallback root
    return dirs


def find_vcvars(explicit=None, which=shutil.which, exists=os.path.exists,
                run=None) -> str:
    """MSVC environment script, or "" when this is not a Windows/MSVC toolchain.

    Ninja + MSVC needs the compiler's environment loaded; CMake alone cannot
    conjure it, so the build is wrapped in ``cmd /c "call vcvars64.bat && ..."``.
    """
    if explicit:
        return str(explicit)
    if platform.system() != "Windows":
        return ""
    if which("cl"):
        return ""                      # a developer prompt is already loaded
    vswhere = os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                           "Microsoft Visual Studio", "Installer",
                           "vswhere.exe")
    if not exists(vswhere):
        return ""
    run = run or _run_capture
    try:
        out = run([vswhere, "-latest", "-products", "*", "-requires",
                   "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                   "-property", "installationPath"]) or ""
    except Exception:
        return ""
    install = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not install:
        return ""
    candidate = os.path.join(install, "VC", "Auxiliary", "Build",
                             "vcvars64.bat")
    return candidate if exists(candidate) else ""


def _run_capture(cmd):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return ""
    return proc.stdout or ""


def host_configure_args(source, build_dir) -> list:
    """CMake argv for the host build (server + rpc server).

    Static backends on purpose. With ggml's shared/dynamic backends the first
    Windows build produced ``llama-server.exe`` that reported "failed to find
    ggml_backend_init" for every DLL and ``Available devices: (none)`` -- it
    compiled and could not run. Static linking removes the dlopen entirely.
    """
    args = ["-S", str(source), "-B", str(build_dir), "-G", "Ninja"]
    for key, value in COMMON_DEFINES.items():
        args.append(f"-D{key}={value}")
    args.append("-DBUILD_SHARED_LIBS=OFF")
    args.append("-DGGML_BACKEND_DL=OFF")
    args.append("-DLLAMA_BUILD_SERVER=ON")
    args.append("-DLLAMA_BUILD_TOOLS=ON")
    return args


def android_configure_args(source, build_dir, ndk, api=28,
                          abi="arm64-v8a") -> list:
    """CMake argv for the Android peripheral (NDK arm64, static STL)."""
    toolchain = os.path.join(str(ndk), "build", "cmake",
                             "android.toolchain.cmake")
    args = ["-S", str(source), "-B", str(build_dir), "-G", "Ninja",
            f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
            f"-DANDROID_ABI={abi}",
            f"-DANDROID_PLATFORM=android-{api}",
            "-DANDROID_STL=c++_static"]
    for key, value in COMMON_DEFINES.items():
        args.append(f"-D{key}={value}")
    # The peripheral only needs the RPC server, and a static STL keeps it
    # self-contained when the app extracts it to nativeLibraryDir.
    args.append("-DGGML_BACKEND_DL=ON")
    args.append("-DBUILD_SHARED_LIBS=ON")
    args.append("-DLLAMA_BUILD_TOOLS=ON")
    args.append("-DLLAMA_BUILD_SERVER=OFF")
    return args


def msvc_env_script(vcvars, python_exe="", dump_path="") -> str:
    """A ``.cmd`` that loads MSVC and writes the resulting environment out.

    Two reasons this is a script rather than an inline ``cmd /c``:

    * a quoted path inside an inline command loses its quotes through Windows
      argv quoting (the first real build failed exactly there);
    * ``set`` wraps very long values, so ``PATH`` came back *empty* and CMake then
      could not find a compiler. Python writes the whole environment to a file as
      one JSON object; the ``set`` dump stays as a fallback.
    """
    python = python_exe or sys.executable or "python"
    lines = ["@echo off",
             f'call "{vcvars}" >nul']
    if dump_path:
        lines.append(f'set > "{dump_path}.set"')
        lines.append(
            f'"{python}" -c "import json,os,sys;'
            f"open(sys.argv[1],'w').write(json.dumps(dict(os.environ)))\" "
            f'"{dump_path}.json"')
    else:
        lines.append("set")
    return "\r\n".join(lines) + "\r\n"


def parse_env_dump(text) -> dict:
    """``KEY=VALUE`` lines from ``set`` output, including wrapped values.

    ``set`` splits a value that is too long across lines, leaving the variable
    name only on the first one; continuation lines belong to the previous key.
    """
    import re
    env = {}
    key = None
    for line in (text or "").splitlines():
        if line.startswith("="):
            key = None          # "=C:=C:\cwd" pseudo-variables, not real ones
            continue
        candidate = line.partition("=")[0].strip()
        if "=" in line and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_()]*", candidate):
            key = candidate
            env[key] = line.partition("=")[2]
        elif key is not None:
            env[key] += line if line.startswith(";") else ";" + line
    return env


def capture_msvc_env(vcvars, run_capture=None, tmp_dir=None,
                     read_text=None, python_exe="") -> dict:
    """Environment variables MSVC needs, or {} when they cannot be read."""
    if not vcvars:
        return {}
    run_capture = run_capture or _run_capture
    read_text = read_text or _read_text
    import json
    import tempfile
    try:
        with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
            script = os.path.join(tmp, "msvc_env.cmd")
            dump = os.path.join(tmp, "env")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(msvc_env_script(vcvars, python_exe=python_exe,
                                             dump_path=dump))
            run_capture(["cmd", "/c", script])
            raw = read_text(dump + ".json", 4_000_000)
            if raw:
                try:
                    data = json.loads(raw)
                except Exception:
                    data = None
                if isinstance(data, dict) and data:
                    return {str(k): str(v) for k, v in data.items()}
            # Fallback: the `set` dump, with wrapped values rejoined.
            return parse_env_dump(read_text(dump + ".set", 4_000_000))
    except Exception:
        return {}


def build_command(cmake, build_dir, targets, jobs=None) -> list:
    """``cmake --build`` argv for the given targets."""
    argv = [str(cmake), "--build", str(build_dir), "--target"]
    argv.extend(targets)
    argv.extend(["--", f"-j{int(jobs)}" if jobs else "-j"])
    return argv


def expected_binaries(build_dir, targets, system=None) -> dict:
    """Where the built executables land, per target (Windows appends .exe)."""
    system = system or platform.system()
    suffix = ".exe" if system == "Windows" else ""
    return {name: str(Path(build_dir) / "bin" / f"{name}{suffix}")
            for name in targets}


# The three questions the next phase depends on, asked of the source itself.
SOURCE_QUESTIONS = {
    # Can a sequence's KV state be written out and read back? That decides how a
    # KV cache can be parked on a peripheral without a new attention algorithm.
    "kv_state_save": ("llama_state_seq_save_file", "llama_state_seq_load_file",
                      "state_seq_save_file", "state_seq_load_file"),
    # Prompt-cache slots on disk are the weaker fallback if the above is absent.
    "slot_save_path": ("slot-save-path", "slot_save_path"),
    # More than one RPC device at once, which is what a multi-phone split needs.
    "rpc": ("--rpc", "ggml_backend_rpc", "rpc_servers"),
}
_SOURCE_ROOTS = ("common", "include", "src", "tools", "ggml")
_SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".h", ".hpp", ".md")


def verify_sources(source_dir, read=None, roots=_SOURCE_ROOTS,
                   max_files=4000, max_bytes=1_500_000) -> dict:
    """Report which capabilities the pinned llama.cpp actually has.

    Never assumed: the answer decides how the KV-cache phase is designed and
    whether several RPC devices can be addressed at once. Returns
    ``{question: [file:marker, ...]}`` plus a ``found`` map.
    """
    read = read or _read_text
    source = Path(source_dir)
    report = {key: [] for key in SOURCE_QUESTIONS}
    report["found"] = {}
    scanned = 0
    for path in sorted(source.rglob("*")):
        if scanned >= max_files:
            break
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        try:
            rel = path.relative_to(source)
        except ValueError:
            continue
        if roots and rel.parts and rel.parts[0] not in roots:
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        text = read(str(path))
        if not text:
            continue
        scanned += 1
        rel_text = rel.as_posix()
        for question, markers in SOURCE_QUESTIONS.items():
            for marker in markers:
                if marker in text:
                    report[question].append(f"{rel_text}:{marker}")
                    report["found"][marker] = True
    report["scanned"] = scanned
    return report


def _read_text(path, limit=400_000):
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def merged_env(extra=None, base=None) -> dict:
    """The process environment plus ``extra`` (subprocess ``env=`` replaces it)."""
    environment = dict(base if base is not None else os.environ)
    if extra:
        environment.update({str(key): str(value)
                            for key, value in extra.items()})
    return environment


def run(argv, runner=None, env=None):
    """Run one build step through the injectable runner."""
    runner = runner or _run
    return runner(argv, env=env)


def _run(argv, env=None):
    """Run a build step, echoing it so the log is reproducible by hand."""
    print("+ " + " ".join(str(part) for part in argv), flush=True)
    proc = subprocess.run(argv, env=merged_env(env))
    return int(proc.returncode)


def _dry_run(argv, env=None):
    print("+ (dry-run) " + " ".join(str(part) for part in argv), flush=True)
    return 0


def parse_available_devices(text) -> list:
    """Device lines from ``llama-server --list-devices`` ([] when none).

    ``(none)`` is the case that matters: it means a build that compiled but can
    offload nowhere, which is exactly what the first Windows build did.
    """
    found = []
    started = False
    for line in (text or "").splitlines():
        if "Available devices" in line:
            started = True
            continue
        if not started:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("("):
            continue
        found.append(stripped)
    return found


def _run_capture_both(cmd, timeout=120):
    """stdout+stderr of a command (the device list can land on either)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except Exception:
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def verify_devices(binary, run_capture=None) -> tuple:
    """(devices, raw output) for a built server -- a built binary is not a
    working one until it can load a backend."""
    run_capture = run_capture or _run_capture_both
    try:
        text = run_capture([str(binary), "--list-devices"]) or ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    return parse_available_devices(text), text.strip()[-600:]


def verify_rpc_flag(binary, run_capture=None) -> bool:
    """True when the built server accepts ``--rpc`` -- the capability that matters.

    ``--list-devices`` is not a usable gate on a statically linked build: it
    reported ``(none)`` for a binary that then loaded the model, held 377 MB of
    layers on the peripheral and returned byte-identical tokens. The flag tells
    the truth about what the build can do.
    """
    run_capture = run_capture or _run_capture_both
    try:
        text = run_capture([str(binary), "--help"]) or ""
    except Exception:
        return False
    return "--rpc" in text


def submodule_commit(repo_root=REPO_ROOT, submodule=SUBMODULE, runner=None):
    """Pinned commit of the llama.cpp submodule ("" when unreadable)."""
    runner = runner or subprocess.run
    try:
        out = runner(["git", "submodule", "status", Path(submodule).as_posix()],
                     cwd=str(repo_root), capture_output=True, text=True)
        return parse_submodule_status(getattr(out, "stdout", "") or "",
                                      Path(submodule).as_posix())
    except Exception:
        return ""


def parse_ls_tree_commit(text) -> str:
    """Submodule commit recorded by the parent repo (``ls-tree`` output)."""
    parts = (text or "").strip().split()
    if len(parts) >= 3 and parts[1] == "commit":
        return parts[2]
    return ""


def submodule_pin(repo_root=REPO_ROOT, submodule=SUBMODULE, runner=None):
    """Commit the parent repo *records* for the submodule ("" when unreadable).

    The checkout can legitimately differ from the pin; the evidence file reports
    both, because a host and a peripheral built from different trees can disagree
    about the wire protocol while every test still passes locally.
    """
    runner = runner or subprocess.run
    try:
        out = runner(["git", "ls-tree", "HEAD", Path(submodule).as_posix()],
                     cwd=str(repo_root), capture_output=True, text=True)
        return parse_ls_tree_commit(getattr(out, "stdout", "") or "")
    except Exception:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", action="store_true", help="build on this machine")
    ap.add_argument("--android", action="store_true",
                    help="cross-build the arm64 peripheral (needs --ndk)")
    ap.add_argument("--ndk", default=os.environ.get("ANDROID_NDK_HOME", ""),
                    help="Android NDK root")
    ap.add_argument("--cmake", default=None)
    ap.add_argument("--ninja", default=None)
    ap.add_argument("--vcvars", default=None)
    ap.add_argument("--host-build", default=str(DEFAULT_HOST_BUILD))
    ap.add_argument("--android-build", default=str(DEFAULT_ANDROID_BUILD))
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--configure-only", action="store_true",
                    help="stop after CMake configure (no compile)")
    ap.add_argument("--verify-api", action="store_true",
                    help="record the KV/RPC source facts in the evidence file")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the commands instead of running them")
    args = ap.parse_args(argv)
    if not (args.host or args.android):
        args.host = True
    runner = _dry_run if args.dry_run else _run

    source = REPO_ROOT / SUBMODULE
    if not args.dry_run and not state_is_populated(source):
        print(f"llama.cpp submodule is empty at {source}\n"
              f"  git submodule update --init {SUBMODULE.as_posix()}",
              file=sys.stderr)
        return 2

    sdk_dirs = android_cmake_dirs()
    cmake = find_tool(("cmake.exe", "cmake"), args.cmake, sdk_dirs)
    if not cmake and not args.dry_run:
        print("no cmake found (install CMake, or an Android SDK that has one)",
              file=sys.stderr)
        return 2
    cmake = cmake or "cmake"
    ninja = find_tool(("ninja.exe", "ninja"), args.ninja, sdk_dirs)
    if ninja and not args.dry_run:
        os.environ["PATH"] = (os.path.dirname(ninja) + os.pathsep
                              + os.environ.get("PATH", ""))
    vcvars = find_vcvars(args.vcvars)
    msvc_env = capture_msvc_env(args.vcvars or vcvars)

    lines = [f"llama.cpp build evidence ({platform.platform()})",
             f"submodule commit: {submodule_commit() or 'unknown'}",
             f"cmake: {cmake}",
             f"ninja: {ninja or 'not found (CMake picks the generator)'}",
             f"msvc env: {vcvars or 'not needed'}",
             ""]
    if vcvars:
        lines.append(f"msvc env vars captured: {len(msvc_env)}")
    pin, checked_out = submodule_pin(), submodule_commit()
    lines.append(f"repo pin: {pin or 'unknown'}")
    if pin and checked_out and pin != checked_out:
        # Both ends must build the same tree; silence here is how a host and a
        # peripheral silently stop agreeing on the protocol.
        lines.append(f"WARNING: checkout {checked_out[:12]} != pin {pin[:12]} "
                     f"- build both ends from this checkout")
    lines.append("")
    if args.verify_api:
        report = verify_sources(source)
        lines.append(f"source facts (scanned {report.get('scanned', 0)} files):")
        for question in SOURCE_QUESTIONS:
            hits = report.get(question) or []
            sample = f" e.g. {hits[0]}" if hits else ""
            verdict = "YES" if hits else "NO"
            lines.append(f"  {question}: {verdict} ({len(hits)} hit(s)){sample}")
        lines.append("")

    status = 0
    for label, enabled in (("host", args.host), ("android", args.android)):
        if not enabled:
            continue
        build_dir = Path(args.host_build if label == "host"
                         else args.android_build)
        targets = HOST_TARGETS if label == "host" else ANDROID_TARGETS
        if label == "android":
            if not args.ndk:
                print("--android needs --ndk <dir> (or ANDROID_NDK_HOME)",
                      file=sys.stderr)
                status = 2
                continue
            configure_args = android_configure_args(source, build_dir, args.ndk)
        else:
            configure_args = host_configure_args(source, build_dir)
        if not args.dry_run:
            build_dir.mkdir(parents=True, exist_ok=True)
        rc = runner([cmake] + configure_args, env=msvc_env)
        if rc != 0:
            print(f"{label}: cmake configure failed ({rc})", file=sys.stderr)
            status = rc
            continue
        if args.configure_only:
            lines.append(f"{label}: configured at {build_dir} (not built)")
            continue
        rc = runner(build_command(cmake, build_dir, targets, args.jobs),
                    env=msvc_env)
        if rc != 0:
            print(f"{label}: build failed ({rc})", file=sys.stderr)
            status = rc
            continue
        produced = expected_binaries(build_dir, targets)
        found = {name: path for name, path in produced.items()
                 if args.dry_run or os.path.exists(path)}
        lines.append(f"{label}: built {sorted(found)} at {build_dir}")
        for name, path in sorted(produced.items()):
            prefix = "  " if name in found else "  MISSING: "
            lines.append(f"{prefix}{name}: {path}")
        if not args.dry_run and len(found) != len(produced):
            status = status or 3
        if label == "host" and "llama-server" in found and not args.dry_run:
            devices, raw = verify_devices(found["llama-server"])
            lines.append(f"  devices: {devices or 'none listed (static build)'}")
            if verify_rpc_flag(found["llama-server"]):
                lines.append("  --rpc: present (this build can offload)")
            else:
                # A server without --rpc cannot take part in a layer split,
                # however cleanly it compiled.
                lines.append("  NO --rpc IN THE BUILT SERVER: it cannot offload")
                lines.append("  raw: " + raw.replace("\n", " | ")[:200])
                status = status or 4

    evidence = REPO_ROOT / EVIDENCE
    if not args.dry_run:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        with open(evidence, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"evidence: {evidence}"
          + (" (not written: dry-run)" if args.dry_run else ""))
    return status


if __name__ == "__main__":
    raise SystemExit(main())



