#!/usr/bin/env python3
"""Layer-split benchmark (Option 2): host RSS and throughput with/without offload.

Reproduces the table in ``docs/layer_split_rpc.md``: it starts a host
``llama-server`` three times (local, half the layers on a peripheral, all of
them) and reports the host's resident memory plus llama.cpp's own prefill and
decode rates.

Needs a host llama.cpp build with RPC and a GGUF:

    cmake -S platforms/android/app/src/main/cpp/llama.cpp -B /tmp/rpc-host \\
      -DCMAKE_BUILD_TYPE=Release -DGGML_RPC=ON -DLLAMA_BUILD_SERVER=ON \\
      -DLLAMA_BUILD_TOOLS=ON
    cmake --build /tmp/rpc-host --target llama-server -j8

The peripheral side is a device's RPC server. Pass ``--serial`` and the script
starts/stops it through the app (a debug broadcast — the adb shell user cannot
exec an app's nativeLibraryDir), so one command reproduces the whole run:

    python3 tests/mesh_rpc_bench.py \\
      --server /tmp/rpc-host/bin/llama-server --model /tmp/qwen0.5b.gguf \\
      --serial adb-R52WC05JPMW-4kMS88._adb-tls-connect._tcp

Or point it at an already-running server with ``--rpc host:port``.

Note the throughput reality this measures: llama.cpp's RPC backend is
synchronous per layer step, so offloading over Wi-Fi trades host memory for
latency (measured 140 -> ~6 tok/s decode). The win is capacity, not speed.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

RPC_ACTION = "com.samurai.shugocore.DEBUG_MESH_RPC"
DEFAULT_HOST_PORT = 8099


def adb_path() -> str:
    return os.environ.get("ADB") or shutil.which("adb") or os.path.expanduser(
        "~/Library/Android/sdk/platform-tools/adb")


def adb(serial: str, *args: str, timeout: float = 60.0):
    try:
        return subprocess.run([adb_path(), "-s", serial, *args],
                              capture_output=True, text=True,
                              timeout=timeout, errors="replace")
    except Exception as exc:
        print(f"adb failed: {exc}", file=sys.stderr)
        return None


def device_lan_ip(serial: str) -> str:
    proc = adb(serial, "shell", "ip", "-4", "addr", "show", "wlan0")
    if proc is None:
        return ""
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", proc.stdout or "")
    return match.group(1) if match else ""


def peripheral_start(serial: str, port: int) -> str:
    """Ask the app to start its RPC peripheral; returns its LAN host."""
    adb(serial, "shell", "am", "broadcast", "-a", RPC_ACTION,
        "--es", "action", "stop")
    time.sleep(1.0)
    adb(serial, "shell", "am", "broadcast", "-a", RPC_ACTION,
        "--es", "action", "start", "--es", "lan", "1",
        "--es", "port", str(port))
    time.sleep(3.0)
    return device_lan_ip(serial)


def peripheral_stop(serial: str) -> None:
    adb(serial, "shell", "am", "broadcast", "-a", RPC_ACTION,
        "--es", "action", "stop")


def setup_usb_forward(serial: str, local_port: int, remote_port: int) -> bool:
    """Tunnel the peripheral's RPC port over adb instead of Wi-Fi.

    Only buys anything when the device is on a USB cable: over wireless adb
    this traverses the same Wi-Fi path. Returns True when the forward is up.
    """
    remove = adb(serial, "forward", "--remove", f"tcp:{local_port}")
    if remove is None or remove.returncode != 0:
        # No stale forward to remove is fine; anything else is reported below.
        pass
    proc = adb(serial, "forward", f"tcp:{local_port}", f"tcp:{remote_port}")
    if proc is None or proc.returncode != 0:
        print(f"forward tcp:{local_port} -> {serial}:50052 failed",
              file=sys.stderr)
        return False
    return True


def remove_usb_forward(serial: str, local_port: int) -> None:
    adb(serial, "forward", "--remove", f"tcp:{local_port}")


def rss_mb(pid: int) -> float:
    try:
        proc = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=10)
        return int((proc.stdout or "0").strip() or 0) / 1024.0
    except Exception:
        return 0.0


def post_json(url: str, payload: dict, timeout: float = 300.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_health(base: str, pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode:
            return False
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def run_config(label: str, argv: list, prompt: str, n_predict: int,
               host_port: int, timeout: float) -> dict:
    """Start llama-server with `argv`, measure it, and stop it."""
    base = f"http://127.0.0.1:{host_port}"
    log_path = f"/tmp/mesh_rpc_bench_{label}.log"
    with open(log_path, "w", encoding="utf-8") as handle:
        proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT)
    try:
        if not wait_health(base, proc.pid, timeout):
            tail = ""
            try:
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    tail = "".join(fh.readlines()[-4:]).strip()[-300:]
            except Exception:
                pass
            return {"label": label, "error": "server did not become healthy",
                    "detail": tail}
        rss = rss_mb(proc.pid)
        try:
            body = post_json(base + "/completion",
                             {"prompt": prompt, "n_predict": n_predict,
                              "temperature": 0.0, "cache_prompt": False})
        except urllib.error.URLError as exc:
            return {"label": label, "error": f"completion failed: {exc}"}
        timings = body.get("timings") or {}
        return {"label": label, "rss_mb": rss,
                "prefill": timings.get("prompt_per_second", 0.0),
                "decode": timings.get("predicted_per_second", 0.0),
                "tokens": body.get("tokens_predicted")}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Layer-split benchmark (host RSS + throughput).")
    parser.add_argument("--server", required=True,
                        help="llama-server binary (built with -DGGML_RPC=ON)")
    parser.add_argument("--model", required=True, help="GGUF to serve")
    parser.add_argument("--rpc", default=None,
                        help="peripheral RPC endpoint host:port (skips --serial)")
    parser.add_argument("--serial", default=None,
                        help="adb serial: start/stop that device's peripheral")
    parser.add_argument("--rpc-port", type=int, default=50052,
                        help="RPC port on the peripheral")
    parser.add_argument("--usb", action="store_true",
                        help="tunnel the RPC port over adb (only buys latency "
                             "when the device is on a USB cable; over wireless "
                             "adb this is still Wi-Fi)")
    parser.add_argument("--usb-local-port", type=int, default=50552,
                        help="local end of the USB forward")
    parser.add_argument("--dev", default="RPC0", help="offload device name")
    parser.add_argument("--layers", type=int, default=24,
                        help="total model layers (for the half split)")
    parser.add_argument("--host-port", type=int, default=DEFAULT_HOST_PORT)
    parser.add_argument("--n-predict", type=int, default=32)
    parser.add_argument("--prompt", default="The capital of France is")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--context", type=int, default=2048)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args(argv)

    if not os.path.isfile(args.server) or not os.path.isfile(args.model):
        print("--server and --model must both exist", file=sys.stderr)
        return 2

    endpoint = args.rpc
    started_serial = ""
    forwarded = False
    if endpoint is None and args.serial:
        host = peripheral_start(args.serial, args.rpc_port)
        if not host:
            print("could not start the peripheral on the device",
                  file=sys.stderr)
            peripheral_stop(args.serial)
            return 1
        if args.usb:
            if not setup_usb_forward(args.serial, args.usb_local_port,
                                     args.rpc_port):
                peripheral_stop(args.serial)
                return 1
            endpoint = f"127.0.0.1:{args.usb_local_port}"
            forwarded = True
        else:
            endpoint = f"{host}:{args.rpc_port}"
        started_serial = args.serial
    if endpoint is None:
        print("provide --rpc host:port or --serial <adb-serial>",
              file=sys.stderr)
        return 2

    common = [args.server, "-m", args.model, "-c", str(args.context),
              "-t", str(args.threads), "--host", "127.0.0.1",
              "--port", str(args.host_port)]
    configs = [
        ("local", []),
        (f"half ({args.layers // 2} layers)", ["--rpc", endpoint,
                                               "-dev", args.dev,
                                               "-ngl", str(args.layers // 2)]),
        (f"all ({args.layers} layers)", ["--rpc", endpoint, "-dev", args.dev,
                                         "-ngl", str(args.layers)]),
    ]

    results = []
    print(f"peripheral: {endpoint}", flush=True)
    for label, extra in configs:
        print(f"  running {label} ...", flush=True)
        results.append(run_config(label, common + extra, args.prompt,
                                  args.n_predict, args.host_port, args.timeout))

    if started_serial:
        peripheral_stop(started_serial)
        if forwarded:
            remove_usb_forward(started_serial, args.usb_local_port)

    print()
    header = f"{'configuration':22} {'host RSS':>10} {'prefill':>10} {'decode':>10}"
    print(header)
    print("-" * len(header))
    for entry in results:
        if entry.get("error"):
            print(f"{entry['label']:22} {entry['error']}: "
                  f"{entry.get('detail', '')[:60]}")
            continue
        print(f"{entry['label']:22} {entry['rss_mb']:8.0f}MB "
              f"{entry['prefill']:9.1f} {entry['decode']:9.2f} tok/s")
    print()
    print("Memory offload is the win; decode falls because llama.cpp's RPC "
          "backend is synchronous per layer step (see docs/layer_split_rpc.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

