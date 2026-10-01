#!/usr/bin/env python3
"""ShugoCore desktop control plane (Windows/macOS/Linux) - the computer
version of the Android app's 5-tab control surface.

Mirrors ``platforms/android/.../ui``: a pinned node-status header over the
SERVER | AGENT | ACTIVITY | SENSORS | SECURITY | LOG panes, polled at 1 Hz.
The backend/model chooser is the point of the SERVER pane: pick how this node
reasons (Ollama, LM Studio / any OpenAI-compatible endpoint, a ShugoCore
server bridge, or the offline stub), detect what that backend actually
serves, and start the node.

Stdlib only (tkinter + urllib) so it runs anywhere the core does -- the same
constraint the rest of the host tooling follows. The agent, its memory, the
policy gates and the mesh are the real thing (``create_agent``); the UI only
reads ``get_status()`` and never fabricates a field the agent did not report.

Usage::

    python clients/desktop/shugocore_desktop.py            # GUI, pick a backend
    python clients/desktop/shugocore_desktop.py --selftest # probe only, no GUI
"""
import argparse
import io
import json
import logging
import os
import queue
import sys
import threading
import time
try:                                    # a host node may have no Tk at all
    import tkinter as tk
    from tkinter import ttk
    TK_AVAILABLE = True
except Exception:                       # headless: --terminal still works
    tk = None
    ttk = None
    TK_AVAILABLE = False
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

POLL_MS = 1000

# Backend choices, mirroring what the server/engine can actually build.
# ``transport`` selects the model-backend config the engine receives:
#   ollama  -> native Ollama wire      (/api/generate)
#   openai  -> OpenAI-compatible /v1   (LM Studio, llama.cpp server, vLLM)
#   bridge  -> a ShugoCore server      (its Ollama-compatible surface)
#   stub    -> deterministic offline
BACKENDS = [
    {"label": "Ollama (native)", "transport": "ollama",
     "url": "http://127.0.0.1:11434", "detect": "/api/tags",
     "hint": "ollama serve; models come from `ollama pull`"},
    {"label": "LM Studio (OpenAI-compatible)", "transport": "openai",
     "url": "http://127.0.0.1:1234", "detect": "/v1/models",
     # LM Studio rejects the legacy json_object mode with HTTP 400
     # ("'response_format.type' must be 'json_schema' or 'text'").
     "json_mode": "json_schema",
     "hint": "LM Studio > Developer > Start Server. URL must NOT include /v1"},
    {"label": "llama.cpp llama-server", "transport": "openai",
     "url": "http://127.0.0.1:8080", "detect": "/v1/models",
     "json_mode": "text",
     "hint": "llama-server --port 8080 --model <gguf>"},
    {"label": "ShugoCore server (bridge)", "transport": "bridge",
     "url": "http://127.0.0.1:11435", "detect": "/api/tags",
     "hint": "shugocore-server --backend openai --backend-url ... --port 11435"},
    {"label": "Stub (offline)", "transport": "stub",
     "url": "", "detect": "", "hint": "no network; exercises gating + memory"},
]

KEY_BACKENDS = [b for b in BACKENDS if b["transport"] == "openai"]

# Consent-gated action types offered as quick picks in the SECURITY pane. A
# convenience list only: the node validates every grant against `policy`'s
# consent-gated families and refuses anything outside them.
CONSENT_QUICK_PICKS = (
    "network_send", "network_query", "network_sync", "fleet_deploy",
    "api_call", "database_update", "hardware_interaction",
    "mobile_request_compute", "robot_navigate",
)


def backend_by_label(label: str) -> dict:
    for spec in BACKENDS:
        if spec["label"] == label:
            return spec
    return BACKENDS[0]


def _http_json(url: str, timeout: float = 6.0, api_key: str = ""):
    # Same rule the model backends apply to a configured endpoint (see
    # model_backends._validated_base_url): http/https with a host, so a
    # mistyped or file:// URL cannot smuggle a local read into the probe.
    # B310 is satisfied by construction rather than by suppressing it.
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"unsupported backend URL {url!r}: http/https only")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8", "replace") or "{}")


def probe_backend(spec: dict, url: str, api_key: str = "") -> dict:
    """Ask the chosen backend what it serves. Returns {ok, detail, models}."""
    url = (url or "").rstrip("/")
    if spec["transport"] == "stub":
        return {"ok": True, "detail": "offline backend; no probe required",
                "models": ["stub-model"]}
    if not url:
        return {"ok": False, "detail": "no base URL set", "models": []}
    started = time.monotonic()
    try:
        payload = _http_json(f"{url}{spec['detect']}", api_key=api_key)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "detail": f"HTTP {exc.code} from {spec['detect']}",
                "models": []}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}",
                "models": []}
    latency_ms = round((time.monotonic() - started) * 1000.0, 1)
    # A third-party endpoint may answer with anything; probing must never
    # raise (a bad payload would otherwise take the UI down).
    if not isinstance(payload, dict):
        return {"ok": True, "models": [], "latency_ms": latency_ms,
                "detail": f"reachable, unexpected payload "
                          f"({type(payload).__name__})"}
    if spec["transport"] == "openai":
        entries, name_key = payload.get("data"), "id"
    else:
        entries, name_key = payload.get("models"), "name"
    if not isinstance(entries, list):
        entries = []
    models = [str(entry.get(name_key, "")) for entry in entries
              if isinstance(entry, dict)]
    models = [model for model in models if model]
    return {"ok": True, "detail": f"{len(models)} model(s) in {latency_ms} ms",
            "models": models, "latency_ms": latency_ms}


def backend_config_for(spec: dict, url: str, model: str) -> dict:
    """The engine-side backend config for a UI choice.

    The agent bootstraps with a hardcoded ``{"type": "android"}`` backend, so a
    choice only takes effect if the engine's model registry is re-pointed (the
    same override ``tests/csfa_soak.py`` uses). Keeping that translation here
    makes it testable without a GUI.
    """
    transport = spec["transport"]
    model = (model or "").strip()
    base = (url or "").rstrip("/")
    if transport == "stub":
        return {"type": "stub"}
    if transport == "ollama":
        return {"type": "ollama", "base_url": base}
    if transport == "openai":
        # base_url must NOT include /v1: the adapter appends
        # "/v1/chat/completions" itself, so ".../v1" yields ".../v1/v1/...".
        if base.endswith("/v1"):
            base = base[:-3]
        config = {"type": "openai", "base_url": base}
        mode = str(spec.get("json_mode") or "").strip()
        if mode:
            # Which structured-output hint the engine may send for a decision.
            config["json_mode"] = mode
        return config
    return {"type": "android", "api_url": base,
            "model_name": model or "shugocore-local"}


class AgentController:
    """Owns the agent and its tick loop in a worker thread.

    Every widget-visible value comes from a snapshot of the real agent; the UI
    thread never touches the agent directly.
    """

    def __init__(self, interval: float = 2.0, sync_interval: float = 0.0):
        self.interval = max(0.5, float(interval))
        # 0 = no periodic mesh sync. The mesh only moves memory when a node
        # asks, so without this the node sees its peers' facts exactly once at
        # boot and every mesh number here goes stale for the life of the
        # process.
        self.sync_interval = max(0.0, float(sync_interval))
        self.agent = None
        self._thread = None
        self._stop = threading.Event()
        self.started_at = 0.0
        self.last_error = ""
        self.log_seq = 0
        self._logs = []
        self.applied = {}
        self.mesh = {}
        self.sync_state = {"rounds": 0, "imported": 0, "failed": 0,
                           "last": ""}

    def start(self, spec: dict, url: str, model: str, api_key: str = "",
              data_dir: str = "runtime/desktop_ui", device_caps: str = "desktop",
              mesh: dict = None) -> str:
        """Build and start a node. Returns "" on success, else the error."""
        from shugocore_agent import create_agent
        mesh = dict(mesh or {})
        data_dir = str(Path(data_dir).expanduser().resolve())
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        if mesh.get("port"):
            os.environ["SHUGOCORE_MESH_PORT"] = str(mesh["port"])
        if mesh.get("peers"):
            os.environ["SHUGOCORE_MESH_PEERS"] = str(mesh["peers"])
        if mesh.get("token"):
            os.environ["SHUGOCORE_MESH_TOKEN"] = str(mesh["token"])
        try:
            # api_url matters even when the model config is re-pointed below:
            # the delegation manager registers it as this node's LOCALHOST
            # backend, and decision_engine overrides a delegated call's
            # base_url with that URL. Leaving it defaulted silently sent every
            # call to Ollama's 11434 regardless of the chosen backend.
            agent = create_agent(device_caps=device_caps,
                                 api_url=url.rstrip("/") or None,
                                 data_dir=data_dir,
                                 mesh_node_id=f"shugo-{device_caps}",
                                 mesh_priority=int(mesh.get("priority", 10)))
        except Exception as exc:                      # never take the UI down
            self.last_error = f"create_agent failed: {exc}"
            return self.last_error
        self.applied = self._apply_backend(agent, spec, url, model)
        self.agent = agent
        self.mesh = dict(mesh)
        self.sync_state = {"rounds": 0, "imported": 0, "failed": 0, "last": ""}
        self.last_error = ""
        self.started_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="node-tick",
                                        daemon=True)
        self._thread.start()
        return ""

    def _apply_backend(self, agent, spec: dict, url: str, model: str) -> dict:
        """Re-point the engine's model registry at the chosen backend."""
        config = backend_config_for(spec, url, model)
        engine = getattr(agent, "engine", None)
        models = getattr(engine, "models", None) if engine is not None else None
        if not isinstance(models, list) or not models:
            return {"backend": config, "model": model,
                    "warning": "engine has no model registry to re-point"}
        models[0]["backend"] = config
        if model:
            models[0]["id"] = model
        cache = getattr(engine, "_backend_cache", None)
        if isinstance(cache, dict):
            cache.clear()
        if url:
            agent.api_url = url.rstrip("/")      # keep status truthful
        return {"backend": config, "model": models[0].get("id", model)}

    # -- run loop ---------------------------------------------------------
    def _loop(self) -> None:
        next_sync = time.monotonic() + self.sync_interval \
            if self.sync_interval > 0 else None
        while not self._stop.is_set():
            agent = self.agent
            if agent is None:
                time.sleep(0.2)
                continue
            try:
                agent.tick()
            except Exception as exc:                  # keep cycling, surface it
                self.last_error = f"tick failed: {exc}"
            if next_sync is not None and time.monotonic() >= next_sync:
                self._sync_peers()
                next_sync = time.monotonic() + self.sync_interval
            self._stop.wait(self.interval)

    def _sync_peers(self) -> None:
        """One periodic pull from every configured peer, on the tick thread.

        Serialized with tick() on purpose: the mesh only moves memory when a
        node asks, and doing it here keeps a sync from racing the model
        backend. A peer that is down is counted, not raised — one bad peer
        must not stop the loop. ``imported: 0`` is a healthy sync on a
        converged fleet, so only failures are reported as such.
        """
        runtime = getattr(self.agent, "shugonet_runtime", None)
        peers = list((getattr(runtime, "_outbound", {}) or {}))
        if runtime is None or not peers:
            return
        imported = failed = 0
        for peer_id in peers:
            try:
                result = runtime.sync(peer_id) or {}
            except Exception as exc:
                failed += 1
                self.sync_state["last"] = f"{peer_id}: {type(exc).__name__}"
                continue
            if str(result.get("status", "")) == "success":
                try:
                    imported += int(result.get("imported", 0) or 0)
                except (TypeError, ValueError):
                    pass
            else:
                failed += 1
                self.sync_state["last"] = f"{peer_id}: {result.get('message', 'error')}"
        self.sync_state["rounds"] += 1
        self.sync_state["imported"] += imported
        self.sync_state["failed"] += failed
        if imported or failed:
            self._logs.append({
                "seq": self.log_seq + 1, "category": "MESH",
                "message": f"sync: +{imported} fact(s) from {len(peers)} peer(s)"
                           + (f", {failed} failed" if failed else ""),
            })
            self.log_seq += 1

    def stop(self) -> None:
        self._stop.set()
        thread, agent = self._thread, self.agent
        if thread is not None and thread.is_alive():
            thread.join(timeout=8.0)
        self._thread = None
        if agent is not None:
            cleanup = getattr(agent, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception as exc:
                    self.last_error = f"cleanup failed: {exc}"
        self.agent = None

    # -- reads ------------------------------------------------------------
    def running(self) -> bool:
        thread = self._thread
        return self.agent is not None and thread is not None \
            and thread.is_alive()

    def _mesh_snapshot(self) -> dict:
        """The live mesh transport state, read from the Shugonet runtime.

        ``get_status()`` alone cannot describe the mesh on a host node: its
        ``mesh_peers`` field is populated from the Android shell's telemetry,
        which no host ever sends, so it stays empty and the UI reports a
        lone node while three peers are connected. The runtime holds the truth
        (``connected_peers``, heartbeat counters, frame stats) — this reads it
        rather than inferring anything, and returns {} when the mesh is off.
        """
        agent = self.agent
        runtime = getattr(agent, "shugonet_runtime", None) if agent else None
        if runtime is None:
            return {}
        try:
            status = runtime.status() or {}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(status, dict):
            return {}
        stats = status.get("stats") or {}
        beat = status.get("heartbeat") or {}
        connected = list(status.get("connected_peers") or [])
        configured = list(status.get("peers") or [])
        return {
            "node_id": status.get("agent_id"),
            "port": status.get("port"),
            "running": bool(status.get("running")),
            "configured_peers": configured,
            "connected_peers": connected,
            "connected_count": len(connected),
            "heartbeat": {
                "interval_s": beat.get("interval_s"),
                "advertising": bool(beat.get("advertising")),
                "handler": bool(beat.get("handler")),
                "heard": beat.get("heard"),
                "rx": stats.get("heartbeats_received"),
                "tx": stats.get("heartbeats_sent"),
            },
            "stats": {
                "sent": stats.get("sent"), "received": stats.get("received"),
                "errors": stats.get("errors"), "imported": stats.get("imported"),
            },
            "sync_watermarks": status.get("sync_watermarks") or {},
        }

    def snapshot(self) -> dict:
        """Everything the UI needs, captured safely for the main thread."""
        agent = self.agent
        status, new_logs, error = {}, [], self.last_error
        if agent is not None:
            try:
                status = agent.get_status() or {}
            except Exception as exc:
                status = {"status_error": str(exc)}
            try:
                new_logs = agent.recent_logs(self.log_seq)
                if new_logs:
                    self.log_seq = int(new_logs[-1].get("seq", self.log_seq))
                    self._logs = (self._logs + new_logs)[-800:]
            except Exception as exc:
                error = error or f"log read failed: {exc}"
        return {"status": status, "logs": new_logs,
                "log_history": list(self._logs), "error": error,
                "running": self.running(), "applied": dict(self.applied),
                "mesh": self._mesh_snapshot(),
                "uptime": (round(time.time() - self.started_at, 1)
                           if self.started_at else 0.0)}


# Defined against Tk when it exists, and against object when this Python has no
# Tk: the module must still import so the --terminal surface can run on a host
# with no windowing system (the GUI branch refuses with an honest message).
class DesktopUI(getattr(tk, "Tk", object)):
    """The Android control plane on the desktop: a pinned status header over
    SERVER | AGENT | ACTIVITY | SENSORS | SECURITY | LOG, polled at 1 Hz."""

    HEADER_ROWS = (("node", "Node"), ("backend", "Backend"),
                   ("model", "Model"), ("engine", "Engine"),
                   ("memory", "Memory"), ("mesh", "Mesh"),
                   ("uptime", "Uptime"), ("ticks", "Ticks"))

    def __init__(self, controller: AgentController, args) -> None:
        super().__init__()
        self.controller = controller
        self.args = args
        self.title("ShugoCore - desktop control plane")
        self.geometry("1000x780")
        self.minsize(860, 620)
        self._hdr = {}
        self._panes = {}
        self._probe = {"ok": None, "detail": "not probed yet", "models": []}
        self._last_log_seq_shown = 0
        # Worker threads must never touch widgets (tkinter is not thread
        # safe): they post ("status"|"probe", payload) here and the 1 Hz poll
        # applies it on the main thread.
        self._ui_queue = queue.Queue()
        self._ui_errors = []
        self._last_source = ""
        self._build_header()
        self._build_tabs()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if args.autostart:
            self.after(500, self._start_node)
        self.after(POLL_MS, self._poll)

    # -- layout helpers ---------------------------------------------------
    def _section(self, parent, text: str) -> ttk.Frame:
        frame = ttk.LabelFrame(parent, text=text, padding=6)
        frame.pack(fill="x", padx=8, pady=4)
        return frame

    def _kv(self, parent, label: str, store: dict, key: str, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           padx=(2, 10))
        value = ttk.Label(parent, text="-")
        value.grid(row=row, column=1, sticky="w")
        store[key] = value

    def _build_header(self) -> None:
        outer = ttk.Frame(self, padding=(10, 8, 10, 0))
        outer.pack(fill="x")
        ttk.Label(outer, text="NODE STATUS",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0,
                                                      sticky="w")
        grid = ttk.Frame(outer)
        grid.grid(row=1, column=0, sticky="we", pady=(4, 0))
        for index, (key, label) in enumerate(self.HEADER_ROWS):
            row, col = divmod(index, 4)
            ttk.Label(grid, text=f"{label}:").grid(row=row, column=col * 2,
                                                   sticky="w", padx=(0, 6))
            value = ttk.Label(grid, text="-")
            value.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 18))
            self._hdr[key] = value
        self._hdr["state"] = ttk.Label(outer, text="node stopped",
                                       foreground="#b3261e")
        self._hdr["state"].grid(row=2, column=0, sticky="w", pady=(6, 0))

    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=8)
        for name in ("SERVER", "AGENT", "ACTIVITY", "SENSORS", "SECURITY",
                     "LOG"):
            frame = ttk.Frame(notebook, padding=6)
            notebook.add(frame, text=name)
            self._panes[name] = frame
        self._build_server(self._panes["SERVER"])
        self._build_agent(self._panes["AGENT"])
        self._build_activity(self._panes["ACTIVITY"])
        self._build_sensors(self._panes["SENSORS"])
        self._build_security(self._panes["SECURITY"])
        self._build_log(self._panes["LOG"])

    # -- polling ----------------------------------------------------------
    def _drain_worker_messages(self) -> None:
        """Apply messages posted by worker threads, on the main thread."""
        while True:
            try:
                kind, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                self._set_status(str(payload))
            elif kind == "probe":
                self._detect_done(payload if isinstance(payload, dict)
                                  else {"ok": False, "models": [],
                                        "detail": str(payload)})

    def _poll(self) -> None:
        snapshot = self.controller.snapshot()
        self._drain_worker_messages()
        self._ui_errors = []
        for name, update in (("header", self._update_header),
                             ("server", self._update_server),
                             ("agent", self._update_agent),
                             ("activity", self._update_activity),
                             ("sensors", self._update_sensors),
                             ("security", self._update_security),
                             ("log", self._append_logs)):
            try:
                update(snapshot)
            except Exception as exc:   # one bad pane must not blank the rest
                self._ui_errors.append(f"{name}: {exc}")
        if self._ui_errors:
            self._hdr["state"].config(
                text="UI error - " + "; ".join(self._ui_errors)[:150],
                foreground="#b3261e")
        self.after(POLL_MS, self._poll)

    def _on_close(self) -> None:
        try:
            self.controller.stop()
        finally:
            self.destroy()

    # -- SERVER pane: backend + model choice ------------------------------
    def _build_server(self, parent) -> None:
        self.var_backend = tk.StringVar(value=self.args.backend)
        self.var_url = tk.StringVar(value=self.args.url)
        self.var_key = tk.StringVar(value=os.environ.get("OPENAI_API_KEY", ""))
        self.var_model = tk.StringVar(value=self.args.model)
        self.var_datadir = tk.StringVar(value=self.args.data_dir)
        self.var_caps = tk.StringVar(value=self.args.device_caps)
        self.var_priority = tk.StringVar(value=str(self.args.mesh_priority))
        self.var_meshport = tk.StringVar(value=str(self.args.mesh_port))
        self.var_peers = tk.StringVar(value=self.args.peers)
        self.var_token = tk.StringVar(
            value=os.environ.get("SHUGOCORE_MESH_TOKEN", ""))

        choice = self._section(parent, "Backend & model")
        ttk.Label(choice, text="Backend").grid(row=0, column=0, sticky="w",
                                               padx=(2, 8))
        combo = ttk.Combobox(choice, textvariable=self.var_backend,
                             values=[b["label"] for b in BACKENDS],
                             state="readonly", width=34)
        combo.grid(row=0, column=1, sticky="w")
        combo.bind("<<ComboboxSelected>>", self._on_backend_change)

        ttk.Label(choice, text="Base URL").grid(row=1, column=0, sticky="w",
                                                padx=(2, 8), pady=(4, 0))
        ttk.Entry(choice, textvariable=self.var_url, width=36).grid(
            row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(choice, text="API key").grid(row=2, column=0, sticky="w",
                                               padx=(2, 8), pady=(4, 0))
        ttk.Entry(choice, textvariable=self.var_key, width=36).grid(
            row=2, column=1, sticky="w", pady=(4, 0))
        ttk.Label(choice, foreground="#555",
                  text="(OpenAI-compatible backends need a value; LM Studio "
                       "accepts anything)").grid(row=3, column=1, sticky="w")

        ttk.Label(choice, text="Model").grid(row=4, column=0, sticky="w",
                                             padx=(2, 8), pady=(6, 0))
        self.cmb_model = ttk.Combobox(choice, textvariable=self.var_model,
                                      width=36)
        self.cmb_model.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Button(choice, text="Detect models",
                   command=self._detect_models).grid(row=4, column=2,
                                                     sticky="w", padx=(8, 0),
                                                     pady=(6, 0))
        self.lbl_hint = ttk.Label(choice, text="", foreground="#555")
        self.lbl_hint.grid(row=5, column=1, columnspan=2, sticky="w")
        self.lbl_endpoint = ttk.Label(choice, text="", foreground="#555")
        self.lbl_endpoint.grid(row=6, column=1, columnspan=2, sticky="w")

        node = self._section(parent, "Node")
        fields = (("Data dir", self.var_datadir, 30),
                  ("Device caps", self.var_caps, 14),
                  ("Mesh priority", self.var_priority, 8),
                  ("Mesh port", self.var_meshport, 8),
                  ("Mesh peers", self.var_peers, 46),
                  ("Mesh token", self.var_token, 46))
        for index, (label, var, width) in enumerate(fields):
            ttk.Label(node, text=label).grid(row=index, column=0, sticky="w",
                                             padx=(2, 8))
            ttk.Entry(node, textvariable=var, width=width).grid(
                row=index, column=1, sticky="w")

        actions = ttk.Frame(parent)
        actions.pack(fill="x", padx=8, pady=(6, 0))
        for text, command in (("Test backend", self._detect_models),
                              ("Start node", self._start_node),
                              ("Apply (restart node)", self._apply_restart),
                              ("Stop node", self._stop_node)):
            ttk.Button(actions, text=text, command=command).pack(side="left",
                                                                 padx=(0, 6))

        health = self._section(parent, "Health")
        self.srv_rows = {}
        rows = (("engine", "Engine ready"), ("probe", "Backend reachable"),
                ("model_call", "Model answering"), ("mesh", "Mesh listening"),
                ("audit", "Audit chain active"), ("governor", "Governor"))
        for index, (key, label) in enumerate(rows):
            ttk.Label(health, text=label).grid(row=index, column=0, sticky="w",
                                               padx=(2, 10))
            value = ttk.Label(health, text="-")
            value.grid(row=index, column=1, sticky="w")
            self.srv_rows[key] = value
        self.srv_status = ttk.Label(parent, text="", foreground="#0b6")
        self.srv_status.pack(fill="x", padx=10, pady=(6, 0))
        self._on_backend_change()
        self._set_status("Pick a backend, detect its models, then Start node.")

    # -- SERVER helpers ---------------------------------------------------
    def _spec(self) -> dict:
        return backend_by_label(self.var_backend.get())

    def _mesh(self) -> dict:
        return {"port": self.var_meshport.get().strip(),
                "peers": self.var_peers.get().strip(),
                "token": self.var_token.get().strip(),
                "priority": self.var_priority.get().strip() or "10"}

    def _on_backend_change(self, _event=None) -> None:
        spec = self._spec()
        known = [b["url"] for b in BACKENDS]
        if not self.var_url.get().strip() or self.var_url.get() in known:
            self.var_url.set(spec["url"])
        self.lbl_hint.config(text=spec["hint"])
        self._update_endpoint_preview()
        self._probe = {"ok": None, "detail": "not probed yet", "models": []}

    def _update_endpoint_preview(self) -> None:
        spec = self._spec()
        url = self.var_url.get().strip().rstrip("/")
        if spec["transport"] == "stub":
            text = "no network calls (deterministic stub)"
        elif spec["transport"] == "openai":
            text = f"will POST {url}/v1/chat/completions"
        else:
            text = f"will POST {url}/api/generate"
        self.lbl_endpoint.config(text=text)

    def _set_status(self, text: str) -> None:
        self.srv_status.config(text=text)

    def _detect_models(self) -> None:
        spec, url = self._spec(), self.var_url.get().strip()
        key = self.var_key.get().strip()
        self._set_status(f"probing {url or '(no URL)'} ...")

        def work():
            self._ui_queue.put(("probe", probe_backend(spec, url, key)))

        threading.Thread(target=work, daemon=True).start()

    def _detect_done(self, result: dict) -> None:
        self._probe = result
        models = list(result.get("models", []))
        self.cmb_model.config(values=models)
        if models and not self.var_model.get().strip():
            self.var_model.set(models[0])
        suffix = f" - {', '.join(models[:6])}" if models else ""
        self._set_status(f"probe: {result.get('detail')}{suffix}")

    def _start_args(self) -> tuple:
        return (self._spec(), self.var_url.get().strip(),
                self.var_model.get().strip(), self.var_key.get().strip(),
                self.var_datadir.get().strip(), self.var_caps.get() or "desktop",
                self._mesh())

    def _start_node(self) -> None:
        if self.controller.running():
            self._set_status("node already running - use Apply to change it")
            return
        spec, url, model, key, data_dir, caps, mesh = self._start_args()
        self._set_status("starting node (engine, memory, mesh) ...")

        def work():
            error = self.controller.start(spec, url, model, key, data_dir,
                                          caps, mesh)
            message = error or (f"node started - {spec['label']}"
                                + (f", model {model}" if model else ""))
            self._ui_queue.put(("status", message))

        threading.Thread(target=work, daemon=True).start()

    def _apply_restart(self) -> None:
        spec, url, model, key, data_dir, caps, mesh = self._start_args()
        self._set_status("restarting node with the chosen backend/model ...")

        def work():
            if self.controller.agent is not None:
                self.controller.stop()
            error = self.controller.start(spec, url, model, key, data_dir,
                                          caps, mesh)
            message = error or (f"restarted on {spec['label']}"
                                + (f" / {model}" if model else ""))
            self._ui_queue.put(("status", message))

        threading.Thread(target=work, daemon=True).start()

    def _stop_node(self) -> None:
        self._set_status("stopping node ...")

        def work():
            self.controller.stop()
            self._ui_queue.put(("status", "node stopped"))

        threading.Thread(target=work, daemon=True).start()

    # -- SERVER updates ---------------------------------------------------
    def _set_health(self, key: str, text: str, good: bool) -> None:
        self.srv_rows[key].config(
            text=text, foreground="#137333" if good else "#b3261e")

    def _update_server(self, snap: dict) -> None:
        status = snap["status"]
        engine_ready = bool(status.get("engine_ready"))
        engine_error = status.get("engine_error")
        self._set_health("engine",
                         str(engine_error) if engine_error
                         else (status.get("engine") or "not built"),
                         engine_ready and not engine_error)
        probe = self._probe
        self._set_health("probe", probe.get("detail", "not probed"),
                         probe.get("ok") is True)
        source = str(status.get("decision_source") or "").strip()
        if source and source != "none":
            self._last_source = source     # transient: resets each cycle
        shown = source if source and source != "none" \
            else (self._last_source or "no decisions yet")
        driven = any(tag in shown for tag in ("model", "shugocore",
                                             "openai", "ollama"))
        self._set_health("model_call", shown, driven)
        role = str(status.get("mesh_role") or "none")
        peers = int(status.get("mesh_peer_count") or 0)
        self._set_health("mesh", f"{role} (peers {peers})",
                         role in ("primary", "follower", "standalone"))
        policy = status.get("policy") or {}
        self._set_health("audit", "active" if policy.get("audit") else "off",
                         bool(policy.get("audit")))
        self._set_health("governor",
                         ("fail-closed" if policy.get("fail_closed")
                          else "unknown")
                         + f"; consent {'required' if policy.get('consent_required') else 'off'}",
                         bool(policy.get("fail_closed")))

    # -- header -----------------------------------------------------------
    def _update_header(self, snap: dict) -> None:
        status, uptime = snap["status"], snap["uptime"]
        applied = snap["applied"]
        backend = (applied.get("backend") or {}).get("type", "-")
        self._hdr["node"].config(text=str(status.get("device_caps") or "-"))
        self._hdr["backend"].config(
            text=f"{backend} {status.get('backend_url') or ''}".strip())
        model = applied.get("model") or "-"
        self._hdr["model"].config(text=model)
        self._hdr["engine"].config(text=str(status.get("engine") or "-"))
        self._hdr["memory"].config(
            text="t0 {a}/t1 {b}/t2 {c}".format(
                a=status.get("tier0_entries", 0),
                b=status.get("tier1_entries", 0),
                c=status.get("tier2_facts", 0)))
        mesh_state = snap.get("mesh") or {}
        role = str(status.get("mesh_role") or "none")
        primary = status.get("mesh_primary")
        connected = mesh_state.get("connected_count")
        if mesh_state.get("error"):
            mesh = f"{role} (mesh unavailable: {mesh_state['error']})"
        elif connected is None:
            # No runtime: say so rather than implying a lone node.
            mesh = f"{role}" + (f" (primary {primary})" if primary else "") \
                   + " - mesh off"
        else:
            mesh = f"{role.upper()}" if role == "primary" else role
            if primary:
                mesh += f" of {primary}"
            mesh += f" - {connected}/{len(mesh_state['configured_peers'] or [])} peers"
        self._hdr["mesh"].config(text=mesh)
        self._hdr["uptime"].config(text=f"{uptime:g}s")
        self._hdr["ticks"].config(text=str(status.get("tick_count", 0)))
        if snap["running"]:
            self._hdr["state"].config(
                text=f"node running (interval {self.controller.interval:g}s)"
                     + (f" | {snap['error']}" if snap["error"] else ""),
                foreground="#137333")
        else:
            self._hdr["state"].config(
                text="node stopped" + (f" | {snap['error']}"
                                       if snap["error"] else ""),
                foreground="#b3261e")

    # -- AGENT pane -------------------------------------------------------
    def _build_agent(self, parent) -> None:
        cycle = self._section(parent, "Current cycle")
        self.agent_rows = {}
        for index, (key, label) in enumerate((
                ("tick", "Tick"), ("inflight", "Task in flight"),
                ("decision", "Last decision"), ("action", "Last action"),
                ("evaluation", "Last evaluation"),
                ("result", "Last cycle result"), ("source", "Decision source"),
                ("model", "Model output sizes"))):
            self._kv(cycle, label, self.agent_rows, key, index)
        stages = self._section(parent, "Pipeline stages")
        self.stage_rows = {}
        try:      # the agent owns the canonical stage list
            from shugocore_agent import PIPELINE_STAGES
            names = list(PIPELINE_STAGES)
        except Exception:
            names = ["OBSERVE", "GATE", "DECIDE", "EVALUATE", "RECORD",
                     "CONSOLIDATE"]
        for index, stage in enumerate(names):
            self._kv(stages, stage, self.stage_rows, stage, index)
        health = self._section(parent, "Validation")
        self.val_rows = {}
        for index, (key, label) in enumerate((
                ("model", "Model answering"), ("tts", "Speech provider"),
                ("memory", "Memory backend"), ("audit", "Audit chain"))):
            self._kv(health, label, self.val_rows, key, index)

    def _update_agent(self, snap: dict) -> None:
        status = snap["status"]
        loop = status.get("loop") or {}
        stages = status.get("loop_stages") or {}
        self.agent_rows["tick"].config(text=str(status.get("tick_count", 0)))
        self.agent_rows["inflight"].config(
            text=str(status.get("task_in_flight")))
        decision = status.get("last_decision")
        if isinstance(decision, dict):
            self.agent_rows["decision"].config(
                text=str(decision.get("action_type") or "-"))
            outputs = decision.get("model_outputs") or {}
        else:
            # last_decision is polymorphic: the engine records a *string*
            # ("no viable action proposed by the model ensemble") when no
            # model proposed one, and a dict otherwise.
            self.agent_rows["decision"].config(text=str(decision or "-")[:70])
            outputs = {}
        self.agent_rows["action"].config(
            text=str(status.get("last_action") or "-")[:70])
        self.agent_rows["evaluation"].config(
            text=str(status.get("last_evaluation") or "-")[:70])
        self.agent_rows["result"].config(
            text=str(status.get("last_cycle_result") or "-")[:70])
        self.agent_rows["source"].config(
            text=str(status.get("decision_source") or "-"))
        self.agent_rows["model"].config(
            text=", ".join(f"{k}: {len(str(v))}" for k, v in outputs.items())
            or "-")
        for stage, label in self.stage_rows.items():
            entry = stages.get(stage)
            if not isinstance(entry, dict):
                entry = {}
            state = str(entry.get("state", "unknown"))
            age = entry.get("age_s")
            label.config(text=state + (f" ({age:g}s)" if age is not None
                                       else ""),
                         foreground="#137333" if state == "ok" else "#b3261e")
        cycles = int(loop.get("cycles", 0))
        self.val_rows["model"].config(
            text=f"{cycles} cycle(s), rate {loop.get('success_rate')}",
            foreground="#137333" if cycles else "#b3261e")
        pipeline = status.get("pipeline") or {}
        self.val_rows["tts"].config(
            text=str(pipeline.get("speech", pipeline.get("tts", "not attached"))))
        self.val_rows["memory"].config(
            text=f"tier2 {status.get('tier2_facts', 0)} fact(s)")
        self.val_rows["audit"].config(
            text=str((status.get("policy") or {}).get("audit")))

    # -- ACTIVITY pane ----------------------------------------------------
    def _build_activity(self, parent) -> None:
        summary = self._section(parent, "Loop")
        self.act_rows = {}
        for index, (key, label) in enumerate((
                ("cycles", "Cycles"), ("rate", "Success rate"),
                ("outcomes", "By outcome"), ("sources", "By source"),
                ("shared", "Shared facts (mesh)"),
                ("peers", "Mesh peers (provenance)"))):
            self._kv(summary, label, self.act_rows, key, index)
        recent = self._section(parent, "Recent cycles")
        self.txt_recent = tk.Text(recent, height=9, wrap="none",
                                  font=("Consolas", 9))
        self.txt_recent.pack(fill="both", expand=True)

    def _update_activity(self, snap: dict) -> None:
        status = snap["status"]
        loop = status.get("loop") or {}
        self.act_rows["cycles"].config(text=str(loop.get("cycles", 0)))
        self.act_rows["rate"].config(text=str(loop.get("success_rate")))
        self.act_rows["outcomes"].config(
            text=json.dumps(loop.get("by_outcome") or {}))
        self.act_rows["sources"].config(
            text=json.dumps(loop.get("by_source") or {}))
        mesh = status.get("mesh_activity") or {}
        self.act_rows["shared"].config(
            text=str(mesh.get("total_shared_facts", 0)))
        peers = mesh.get("peers") or []
        self.act_rows["peers"].config(
            text=", ".join(str(p.get("peer", p)) if isinstance(p, dict)
                           else str(p) for p in peers[:8]) or "-")
        lines = [json.dumps(e)[:180] if isinstance(e, dict) else str(e)[:180]
                 for e in (loop.get("recent") or [])[-12:]]
        text = "\n".join(lines)
        if text != self.txt_recent.get("1.0", "end-1c"):
            self.txt_recent.delete("1.0", "end")
            self.txt_recent.insert("1.0", text)

    # -- SENSORS pane -----------------------------------------------------
    def _build_sensors(self, parent) -> None:
        note = self._section(parent, "Local sensors")
        ttk.Label(note, foreground="#555", wraplength=900, justify="left",
                  text="Host sensors are declared, never assumed: a row appears "
                       "only after the agent has ACKed a capability "
                       "declaration. Desktop hosts (macOS/Windows/Linux) can "
                       "offer camera and microphone, but no host-side provider "
                       "is wired yet, so they read 'none declared' until one "
                       "is added. Remote sensors arrive from mesh peers."
                  ).pack(anchor="w")
        rows = self._section(parent, "Declared capabilities")
        self.sensor_rows = {}
        for index, (key, label) in enumerate((("declared", "Declared"),
                                              ("telemetry", "Telemetry seen"),
                                              ("peers", "Mesh peers"))):
            self._kv(rows, label, self.sensor_rows, key, index)
        mesh = self._section(parent, "Mesh membership")
        for index, (key, label) in enumerate((
                ("mesh_node", "This node"),
                ("mesh_role", "Role"),
                ("mesh_primary", "Primary lease"),
                ("mesh_conn", "Connected peers"),
                ("mesh_beat", "Heartbeat"),
                ("mesh_frames", "Frames sent / received"),
                ("mesh_imported", "Facts imported from mesh"),
                ("mesh_sync", "Sync schedule"),
                ("mesh_errors", "Transport errors"))):
            self._kv(mesh, label, self.sensor_rows, key, index)

    def _update_sensors(self, snap: dict) -> None:
        status = snap["status"]
        caps = status.get("capabilities") or {}
        declared = [key for key, value in caps.items()
                    if isinstance(value, dict)
                    and (value.get("granted") or value.get("declared")
                         or value.get("agent_ack"))]
        self.sensor_rows["declared"].config(
            text=", ".join(declared[:10]) or "none declared")
        self.sensor_rows["telemetry"].config(
            text=str(bool(status.get("telemetry_received"))))
        peers = status.get("mesh_peers") or []
        names = [str(p.get("device_id", p.get("id", p)))
                 if isinstance(p, dict) else str(p) for p in peers[:8]]
        self.sensor_rows["peers"].config(text=", ".join(names) or "none")
        # Mesh membership: connected peers come from the runtime, because
        # get_status().mesh_peers is fed by the Android shell and is always
        # empty on a host.
        mesh = snap.get("mesh") or {}
        if not mesh or mesh.get("error"):
            detail = ("mesh off" if not mesh
                      else f"unavailable: {mesh.get('error')}")
            for key in ("mesh_node", "mesh_role", "mesh_primary", "mesh_conn",
                        "mesh_beat", "mesh_frames", "mesh_imported",
                        "mesh_sync", "mesh_errors"):
                self.sensor_rows[key].config(text=detail)
            return
        beat = mesh.get("heartbeat") or {}
        stats = mesh.get("stats") or {}
        self.sensor_rows["mesh_node"].config(
            text=f"{mesh.get('node_id')} (port {mesh.get('port')})")
        role = str(status.get("mesh_role") or "unknown")
        self.sensor_rows["mesh_role"].config(
            text=role.upper() if role == "primary" else role)
        self.sensor_rows["mesh_primary"].config(
            text=str(status.get("mesh_primary") or "none"))
        connected = mesh.get("connected_peers") or []
        self.sensor_rows["mesh_conn"].config(
            text=", ".join(connected) or "none")
        advert = "advertising" if beat.get("advertising") else "idle"
        self.sensor_rows["mesh_beat"].config(
            text=f"{advert} every {beat.get('interval_s')}s "
                 f"(heard {beat.get('heard')}, rx {beat.get('rx')}, "
                 f"tx {beat.get('tx')})")
        self.sensor_rows["mesh_frames"].config(
            text=f"{stats.get('sent')} / {stats.get('received')}")
        self.sensor_rows["mesh_imported"].config(
            text=str(stats.get("imported")))
        errors = stats.get("errors")
        self.sensor_rows["mesh_errors"].config(
            text=str(errors), foreground="#137333" if not errors else "#b3261e")
        sync = getattr(self.controller, "sync_state", None) or {}
        interval = getattr(self.controller, "sync_interval", 0.0)
        if interval > 0:
            self.sensor_rows["mesh_sync"].config(
                text=f"every {interval:g}s, {sync.get('rounds', 0)} round(s), "
                     f"+{sync.get('imported', 0)} fact(s), "
                     f"{sync.get('failed', 0)} failed")
        else:
            self.sensor_rows["mesh_sync"].config(
                text="off - peer facts are pulled once at boot only")

    # -- SECURITY pane ----------------------------------------------------
    def _build_security(self, parent) -> None:
        policy = self._section(parent, "Governor")
        self.sec_rows = {}
        for index, (key, label) in enumerate((
                ("fail_closed", "Fail closed"), ("consent", "Consent required"),
                ("audit", "Audit chain"), ("caps", "Agent capabilities"),
                ("network", "Network policy"))):
            self._kv(policy, label, self.sec_rows, key, index)
        audit = self._section(parent, "Audit chain")
        ttk.Button(audit, text="Verify audit chain",
                   command=self._verify_audit).grid(row=0, column=0,
                                                    sticky="w", padx=(2, 10))
        self.sec_rows["audit_check"] = ttk.Label(audit, text="not verified")
        self.sec_rows["audit_check"].grid(row=0, column=1, sticky="w")
        baseline = self._section(parent, "Security baseline (Track 2)")
        self.sec_rows["baseline"] = ttk.Label(baseline, text="-",
                                              wraplength=900, justify="left")
        self.sec_rows["baseline"].pack(anchor="w")
        consent = self._section(parent, "Operator consent (Track 1)")
        row = ttk.Frame(consent)
        row.pack(anchor="w")
        ttk.Label(row, text="Action").pack(side="left")
        self.var_consent_action = tk.StringVar(value="network_send")
        ttk.Combobox(row, textvariable=self.var_consent_action, width=26,
                     values=CONSENT_QUICK_PICKS).pack(side="left", padx=6)
        ttk.Label(row, text="TTL (s)").pack(side="left")
        self.var_consent_ttl = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.var_consent_ttl, width=8).pack(
            side="left", padx=6)
        ttk.Button(row, text="Grant", command=self._grant_consent).pack(
            side="left", padx=(2, 4))
        ttk.Button(row, text="Revoke", command=self._revoke_consent).pack(
            side="left")
        self.sec_rows["consent_msg"] = ttk.Label(
            consent, text="blank TTL = no expiry; grants are operator-issued",
            wraplength=900, justify="left")
        self.sec_rows["consent_msg"].pack(anchor="w")
        self.sec_rows["grants"] = ttk.Label(consent, text="-", wraplength=900,
                                            justify="left")
        self.sec_rows["grants"].pack(anchor="w")

    def _verify_audit(self) -> None:
        path = os.path.join(self.var_datadir.get().strip(), "audit_chain.jsonl")
        try:
            from audit import verify_audit_file
            ok = bool(verify_audit_file(path))
            self.sec_rows["audit_check"].config(
                text=("chain OK" if ok else "CHAIN INVALID") + f"  ({path})",
                foreground="#137333" if ok else "#b3261e")
        except Exception as exc:
            self.sec_rows["audit_check"].config(
                text=f"verify failed: {exc}", foreground="#b3261e")

    # -- operator consent (SECURITY pane) --------------------------------

    def _consent_registry(self):
        """The registry the decision gate consults (engine registry first)."""
        engine = getattr(self.agent, "engine", None)
        return (getattr(engine, "consents", None)
                or getattr(self.agent, "consents", None))

    def _consent_note(self, text: str, error: bool = False) -> None:
        label = self.sec_rows.get("consent_msg")
        if label is not None:
            label.config(text=text,
                         foreground="#b3261e" if error else "#137333")

    def _grant_consent(self) -> None:
        action = self.var_consent_action.get().strip()
        ttl_raw = self.var_consent_ttl.get().strip()
        ttl = None
        if ttl_raw:
            try:
                ttl = float(ttl_raw)
            except ValueError:
                self._consent_note("TTL must be a number of seconds",
                                   error=True)
                return
            if ttl <= 0:
                self._consent_note("TTL must be positive", error=True)
                return
        registry = self._consent_registry()
        if registry is None:
            self._consent_note("this node has no consent registry",
                               error=True)
            return
        try:
            registry.grant(
                action_type=action, granted_by="desktop-ui",
                note="operator grant from the desktop control plane",
                ttl_seconds=ttl)
        except Exception as exc:
            self._consent_note(f"grant failed: {exc}", error=True)
            return
        self._consent_note(f"granted '{action}'"
                           + (f" for {ttl:g}s" if ttl else " (no expiry)"))
        self._refresh_grants()

    def _revoke_consent(self) -> None:
        action = self.var_consent_action.get().strip()
        registry = self._consent_registry()
        if registry is None:
            self._consent_note("this node has no consent registry",
                               error=True)
            return
        try:
            removed = int(registry.revoke(action))
        except Exception as exc:
            self._consent_note(f"revoke failed: {exc}", error=True)
            return
        self._consent_note(f"revoked {removed} grant(s) for '{action}'")
        self._refresh_grants()

    def _refresh_grants(self) -> None:
        label = self.sec_rows.get("grants")
        registry = self._consent_registry()
        if label is None or registry is None:
            return
        try:
            snapshot = registry.grants() or {}
        except Exception as exc:
            label.config(text=f"grants unavailable: {exc}",
                         foreground="#b3261e")
            return
        if not snapshot:
            label.config(text="no grants in force - consent-gated actions are "
                              "refused (fail-closed)", foreground="#444")
            return
        parts = [f"{action}({len(entries)})"
                 for action, entries in sorted(snapshot.items())]
        label.config(text="in force: " + ", ".join(parts), foreground="#444")

    def _update_security(self, snap: dict) -> None:
        policy = snap["status"].get("policy") or {}
        self.sec_rows["fail_closed"].config(text=str(policy.get("fail_closed")))
        self.sec_rows["consent"].config(text=str(policy.get("consent_required")))
        self.sec_rows["audit"].config(text=str(policy.get("audit")))
        caps = policy.get("agent_caps") or {}
        self.sec_rows["caps"].config(
            text=", ".join(f"{k}={'on' if v else 'off'}"
                           for k, v in sorted(caps.items())[:10]) or "-")
        self.sec_rows["network"].config(
            text=json.dumps(policy.get("network") or {}))
        baseline = snap["status"].get("security_baseline")
        self.sec_rows["baseline"].config(
            text=json.dumps(baseline)[:600] if isinstance(baseline, dict)
            else "not reported by this node")
        self._refresh_grants()

    # -- LOG pane ---------------------------------------------------------
    def _build_log(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=(4, 2))
        ttk.Label(bar, text="Level").pack(side="left")
        self.var_level = tk.StringVar(value="ALL")
        combo = ttk.Combobox(bar, textvariable=self.var_level, width=8,
                             state="readonly",
                             values=("ALL", "INFO", "WARN", "ERROR"))
        combo.pack(side="left", padx=6)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._rerender_logs())
        self.lbl_log_count = ttk.Label(bar, text="0 entries")
        self.lbl_log_count.pack(side="left", padx=10)
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        scroll = ttk.Scrollbar(frame)
        scroll.pack(side="right", fill="y")
        self.txt_log = tk.Text(frame, height=16, wrap="none",
                               font=("Consolas", 9),
                               yscrollcommand=scroll.set)
        self.txt_log.pack(fill="both", expand=True)
        scroll.config(command=self.txt_log.yview)
        self._log_total = 0

    @staticmethod
    def _format_log(entry: dict) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(entry.get("ts", 0)))
        return "{stamp} {lvl:<5} {cat:<9} {msg}\n".format(
            stamp=stamp, lvl=str(entry.get("level", ""))[:5],
            cat=str(entry.get("category", ""))[:9],
            msg=str(entry.get("message", "")))

    def _append_logs(self, snap: dict) -> None:
        new = snap.get("logs") or []
        level = self.var_level.get()
        if new:
            self._log_total += len(new)
            for entry in new:
                if level != "ALL" and str(entry.get("level")) != level:
                    continue
                self.txt_log.insert("end", self._format_log(entry))
            self.txt_log.see("end")
        buffered = len(snap.get("log_history") or [])
        self.lbl_log_count.config(text=f"{self._log_total} seen / "
                                       f"{buffered} buffered")

    def _rerender_logs(self) -> None:
        self.txt_log.delete("1.0", "end")
        level = self.var_level.get()
        for entry in self.controller._logs[-400:]:
            if level != "ALL" and str(entry.get("level")) != level:
                continue
            self.txt_log.insert("end", self._format_log(entry))
        self.txt_log.see("end")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shugocore-desktop",
        description="ShugoCore desktop control plane (node + backend/model "
                    "selection).")
    parser.add_argument("--backend", default=BACKENDS[0]["label"],
                        help="initial backend choice (any label from BACKENDS)")
    parser.add_argument("--url", default="", help="backend base URL")
    parser.add_argument("--model", default="", help="model id to request")
    parser.add_argument("--data-dir", default="runtime/desktop_ui",
                        help="node state dir (memory db, audit chain)")
    parser.add_argument("--device-caps", default="desktop")
    parser.add_argument("--mesh-priority", type=int, default=10)
    parser.add_argument("--mesh-port", type=int, default=9000)
    parser.add_argument("--peers", default="", help="id=host:port,...")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between node ticks")
    parser.add_argument("--sync-interval", type=float, default=0.0,
                        help="seconds between mesh syncs; 0 disables (the mesh "
                             "only moves memory when a node asks, so without "
                             "this the node sees a peer's facts once at boot "
                             "and every mesh figure goes stale)")
    parser.add_argument("--autostart", action="store_true",
                        help="start the node immediately on launch")
    parser.add_argument("--selftest", action="store_true",
                        help="probe the backends and exit (no GUI)")
    parser.add_argument("--terminal", action="store_true",
                        help="operator engagement terminal: type to the agent and "
                             "watch what it heard, what it decided and who "
                             "answered (no GUI; runs over SSH, needs no Tk)")
    parser.add_argument("--exit-after", type=float, default=0.0,
                        help="with --terminal, exit after N seconds (0 = run "
                             "until Ctrl+C or /quit); used by the smoke test")
    parser.add_argument("--no-input", action="store_true",
                        help="with --terminal, display only (do not read stdin)")
    parser.add_argument("--voice", action="store_true",
                        help="with --terminal, speak replies aloud when this node "
                             "has a voice (Windows System.Speech); printing is "
                             "unaffected, so a node without one still answers")
    parser.add_argument("--voice-rate", type=int, default=None,
                        help="with --voice, speech rate -10..10 (default: the "
                             "voice's own rate)")
    parser.add_argument("--voice-engine", default="", metavar="NAME",
                        help="with --voice, a named System.Speech voice to use")
    parser.add_argument("--mesh-token", default="",
                        help="mesh shared secret for --terminal (defaults to "
                             "SHUGOCORE_MESH_TOKEN)")
    parser.add_argument("--say", action="append", default=[], metavar="TEXT",
                        help="with --terminal, submit TEXT as a typed turn at "
                             "startup (repeatable; for scripting and smoke tests "
                             "-- no stdin needed)")
    parser.add_argument("--verbose", action="store_true",
                        help="with --terminal, keep the agent's INFO logging on "
                             "the console as well as in the log pane stream")
    return parser


def selftest(args) -> int:
    """Headless check: backend/config translation + live probes.

    Each backend is probed at its *own* default URL (an explicit ``--url`` is
    probed separately) -- probing them all through one URL would report one
    backend's reachability under another's name.
    """
    print("backend config translation:")
    for spec in BACKENDS:
        print(f"  {spec['label']:<34} -> "
              f"{backend_config_for(spec, spec['url'] or 'http://x', 'm')}")
    print("\nlive probes:")
    targets = [(spec, spec["url"]) for spec in BACKENDS]
    explicit = str(getattr(args, "explicit_url", "") or "").strip()
    if explicit:
        targets.append((backend_by_label(args.backend), explicit))
    failures = probeable = 0
    for spec, url in targets:
        if spec["transport"] == "stub":
            print(f"  {spec['label']:<34} skipped (offline backend)")
            continue
        probeable += 1
        result = probe_backend(spec, url, os.environ.get("OPENAI_API_KEY", ""))
        if not result["ok"]:
            failures += 1
        print(f"  {spec['label']:<34} {'OK  ' if result['ok'] else 'FAIL'} "
              f"{url} - {result['detail']}")
        for model in result.get("models", [])[:4]:
            print(f"        model: {model}")
    print(f"\n{probeable - failures}/{probeable} backend(s) reachable")
    return 0 if failures == 0 else 1


class WindowsVoice:
    """Speak replies aloud on this node, through whatever voice Windows has.

    The engine is PowerShell's ``System.Speech``, driven as a subprocess so the
    Python side stays stdlib-only and a node without a voice loses nothing but the
    sound. The sentence goes to the child in the *environment*, never on the
    command line: a reply containing quotes, semicolons or a pipeline is a sentence
    to be spoken, not something for a shell to parse.

    ``say()`` hands one sentence to a worker thread and returns, because the tick
    loop must never wait on a loudspeaker -- and the queue counts what it drops
    rather than swallowing a reply in silence.
    """

    POWERSHELL = "powershell.exe"
    SETUP = ("Add-Type -AssemblyName System.Speech; "
             "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
             "if ($env:SHUGO_SPEAK_RATE) { $s.Rate = [int]$env:SHUGO_SPEAK_RATE }; "
             "if ($env:SHUGO_SPEAK_VOICE) { "
             "try { $s.SelectVoice($env:SHUGO_SPEAK_VOICE) } catch { } }; ")
    PROBE = ("Add-Type -AssemblyName System.Speech; "
             "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
             "Write-Output ('ok|' + $s.Voice.Name)")
    SAY = SETUP + "$s.Speak($env:SHUGO_SPEAK_TEXT); Write-Output 'spoken'"
    WAVE = (SETUP + "$s.SetOutputToWaveFile($env:SHUGO_SPEAK_WAV); "
            "$s.Speak($env:SHUGO_SPEAK_TEXT); $s.SetOutputToNull(); "
            "Write-Output 'written'")

    def __init__(self, rate=None, voice="", timeout=30.0, runner=None,
                 powershell=None) -> None:
        self.rate = rate
        self.voice = voice or ""
        self.timeout = timeout
        self.powershell = powershell or self.POWERSHELL
        self._runner = runner or self._run_powershell
        self._available = None
        self.reason = "not probed yet"
        self.voice_name = ""
        self.spoken = 0
        self.dropped = 0
        self.speaking = False
        self._queue = queue.Queue(maxsize=8)
        self._worker = None
        self._attention = None

    # -- talking to the engine ------------------------------------------
    def _run_powershell(self, script, env):
        import subprocess
        merged = dict(os.environ)
        merged.update(env)
        return subprocess.run([self.powershell, "-NoProfile", "-NonInteractive",
                               "-Command", script],
                              env=merged, capture_output=True, text=True,
                              timeout=self.timeout, check=False)

    def _run(self, script, env=None):
        try:
            return self._runner(script, dict(env or {}))
        except Exception as exc:            # no engine on this node is not an error
            self.reason = f"{type(exc).__name__}: {exc}"
            return None

    def probe(self) -> bool:
        """Is there a voice on this node? Asked once; the answer says why not."""
        if self._available is not None:
            return self._available
        result = self._run(self.PROBE)
        if result is None:
            self._available = False
            return False
        if result.returncode != 0:
            self._available = False
            detail = (result.stderr or result.stdout or "").strip()
            self.reason = detail[:200] or f"powershell exited {result.returncode}"
            return False
        lines = (result.stdout or "").strip().splitlines()
        last = lines[-1] if lines else ""
        if not last.startswith("ok"):
            self._available = False
            self.reason = f"no voice reported ({last[:80] or 'no output'})"
            return False
        self.voice_name = last.split("|", 1)[1].strip() if "|" in last else ""
        self._available = True
        self.reason = "ready"
        return True

    def describe(self) -> str:
        label = (f"System.Speech ({self.voice_name})" if self.voice_name
                 else "System.Speech")
        if self.rate not in (None, ""):
            label += f" rate {self.rate}"
        return label


    # -- saying something ----------------------------------------------
    def attach(self, attention) -> None:
        """Tell the agent's arbiter when this node talks, so it can be cut off."""
        self._attention = attention

    def _stamp(self, speaking: bool) -> None:
        stamp = getattr(self._attention, "stamp_tts", None)
        if callable(stamp):
            try:
                stamp(bool(speaking))
            except Exception:
                pass
        self.speaking = bool(speaking)

    def enqueue(self, text) -> bool:
        """Hand one sentence to the speaker thread. False when it could not go."""
        message = str(text or "").strip()
        if not message or not self.probe():
            return False
        self._ensure_worker()
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            self.dropped += 1
            return False
        return True

    def say(self, text) -> bool:
        """The speaker-facing name for one spoken sentence."""
        return self.enqueue(text)

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._drain, name="terminal-voice",
                                        daemon=True)
        self._worker.start()

    def _drain(self) -> None:
        while True:
            message = self._queue.get()
            try:
                self._speak_now(message)
            except Exception as exc:        # one bad sentence, not a dead voice
                self.reason = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()

    def _env(self, **extra) -> dict:
        values = {"SHUGO_SPEAK_RATE":
                  "" if self.rate in (None, "") else str(self.rate),
                  "SHUGO_SPEAK_VOICE": self.voice}
        values.update({key: str(value) for key, value in extra.items()})
        return values

    def _speak_now(self, message: str) -> bool:
        self._stamp(True)
        result = None
        try:
            result = self._run(self.SAY, self._env(SHUGO_SPEAK_TEXT=message))
            ok = result is not None and result.returncode == 0
        finally:
            self._stamp(False)
        if ok:
            self.spoken += 1
        else:
            detail = "" if result is None else \
                (result.stderr or result.stdout or "").strip()[:200]
            self.reason = detail or "the voice engine did not report success"
        return ok

    def synthesize(self, text, path) -> bool:
        """Write one sentence to a WAV file instead of the loudspeaker.

        Same engine and same text path, but the result is an artifact that can be
        measured -- which is how a voice the machine running the test cannot
        *hear* is still proven to work.
        """
        message = str(text or "").strip()
        if not message or not self.probe():
            return False
        result = self._run(self.WAVE,
                           self._env(SHUGO_SPEAK_TEXT=message,
                                     SHUGO_SPEAK_WAV=str(path)))
        if result is None or result.returncode != 0:
            detail = "" if result is None else \
                (result.stderr or result.stdout or "").strip()[:200]
            self.reason = detail or "the voice engine did not report success"
            return False
        try:
            return Path(path).stat().st_size > 0
        except OSError:
            return False


class TerminalSpeaker:
    """Deliver the agent's replies to the operator sitting at this terminal.

    The agent's response router asks every candidate node whether it can
    ``speak()`` and takes the closest one that can; ``can_speak`` has always meant
    "the reply reaches a human". A terminal delivers by printing, and says so:
    nothing here claims audio, and a device standing nearer the operator with a
    real speaker still wins.
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stdout
        self.delivered = 0
        self.last = ""
        self._voice = None

    def attach_voice(self, voice) -> None:
        """Give the terminal a loudspeaker as well as a screen.

        Printing stays the delivery: ``can_speak`` has always meant "the reply
        reaches a human", and sight counts. A voice adds sound when the node has
        one, and a node without one is not a node that cannot answer -- so nothing
        downstream of this method changes its mind about who should speak.
        """
        self._voice = voice

    def speak(self, text: str) -> bool:
        message = str(text or "").strip()
        if not message:
            return False
        self.delivered += 1
        self.last = message
        print(f"\nAgent> {message}\n", file=self._stream, flush=True)
        if self._voice is not None:
            # Queued, not awaited: the tick loop must not wait on a loudspeaker.
            self._voice.say(message)
        return True


def handle_terminal_command(agent, text) -> bool:
    """The terminal's slash commands. True when the line was one of them.

    ``/say`` drives one speech action through the agent's *own* gated path -- the
    same one the AGENT tab's "Test speech" control uses -- instead of speaking from
    the terminal, so anything this node says out loud has passed the same policy
    gate as everything else it says. The terminal prints and the agent speaks; the
    terminal never decides to.
    """
    lowered = str(text or "").strip().lower()
    if lowered == "/status":
        status = agent.get_status() or {}
        for key in ("node_id", "mesh_role", "model", "security_baseline"):
            if key in status:
                print(f"    {key:<18} {status[key]}", flush=True)
        return True
    if lowered == "/say" or lowered.startswith("/say "):
        asked = str(text).strip()[4:].strip()
        try:
            result = agent.speak_test(asked or None)
        except Exception as exc:            # a bad command must not kill the loop
            print(f"    speak_test failed: {type(exc).__name__}: {exc}", flush=True)
            return True
        if isinstance(result, dict):
            summary = ", ".join(f"{key}={value}" for key, value in result.items())
        else:
            summary = str(result)
        print(f"    speak_test -> {summary or 'no result'}", flush=True)
        return True
    return False


def run_terminal(args) -> int:
    """The engagement terminal: a conversation with the agent in a console.

    One node, one tick thread, one turn pipeline -- the same AgentController the
    GUI drives, so this is a second *front end* rather than a second agent. Typed
    words go in through the agent's own conversational path and are labelled
    ``terminal``, so nothing downstream can record them as something a microphone
    heard. Speech in comes from the mesh: a phone's on-device recogniser already
    streams its transcripts to the primary, and this node is a peer.
    """
    spec = backend_by_label(args.backend)
    try:
        # A surface whose output only appears when it exits is not a terminal:
        # redirected stdout is block-buffered by default, so a scripted run (or a
        # session piped to a file) would show nothing until the process ended.
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    controller = AgentController(interval=args.interval,
                                 sync_interval=args.sync_interval)
    mesh = {"port": args.mesh_port, "peers": args.peers,
            "priority": args.mesh_priority,
            "token": args.mesh_token or os.environ.get("SHUGOCORE_MESH_TOKEN", "")}
    error = controller.start(spec, args.url, args.model, data_dir=args.data_dir,
                             device_caps=args.device_caps, mesh=mesh)
    if error:
        print(f"node failed to start: {error}", file=sys.stderr)
        return 1
    agent = controller.agent
    speaker = TerminalSpeaker()
    voice = None
    voice_note = "off (pass --voice to speak replies aloud here)"
    if getattr(args, "voice", False):
        voice = WindowsVoice(rate=getattr(args, "voice_rate", None),
                             voice=getattr(args, "voice_engine", "") or "")
        if voice.probe():
            # The arbiter is told while this node is talking, so a voice here is
            # interruptible like any other: sound is not a claim on the room.
            voice.attach(getattr(agent, "attention", None))
            speaker.attach_voice(voice)
        else:
            voice_note = (f"unavailable on this node ({voice.reason}); "
                          "replies still print")
            voice = None
    agent.register_speak_listener(speaker)
    if not getattr(args, "verbose", False):
        # Set *after* the node boots: the agent's own logging setup configures the
        # root logger, so a level set before it is silently overwritten -- which is
        # what put a second, rawer copy of every INFO line on the console once per
        # second. Every agent line still reaches the operator below, through the
        # controller's snapshot, which is the curated view.
        logging.getLogger().setLevel(logging.WARNING)

    print("ShugoCore engagement terminal")
    print(f"  node      shugo-{args.device_caps}   backend {args.backend}"
          f" -> {args.url or spec['url'] or 'offline stub'}")
    print(f"  data dir  {Path(args.data_dir).expanduser().resolve()}")
    print(f"  mesh      port {args.mesh_port}  peers {args.peers or '(none)'}")
    print("  hearing   a phone in the mesh streams its on-device transcripts "
          "here; typed words are labelled 'terminal', never 'heard'")
    print(f"  voice     {voice.describe() if voice is not None else voice_note}")
    print("  commands  /status  /say TEXT  /quit      (Ctrl+C also exits)\n")

    stop = threading.Event()

    def _submit(text: str) -> None:
        """One typed turn, through the agent's own pipeline. Shared by --say and
        by the interactive reader so a scripted run and a session do the same
        thing."""
        text = str(text or "").strip()
        if not text:
            return
        if handle_terminal_command(agent, text):
            return
        try:
            handled = agent.handle_typed_input(text)
        except Exception as exc:                 # a bad turn must not kill the loop
            print(f"  (turn failed: {type(exc).__name__}: {exc})", flush=True)
            return
        if not handled:
            print("  (nothing to handle)", flush=True)

    for line in (getattr(args, "say", None) or []):
        _submit(str(line))

    if not args.no_input and sys.stdin is not None:
        def _reader() -> None:
            prompt = "you> " if sys.stdin.isatty() else ""
            while not stop.is_set():
                try:
                    line = input(prompt)
                except (EOFError, KeyboardInterrupt):
                    return                      # EOF closes input, not the node
                text = str(line or "").strip()
                if text in ("/quit", "/exit"):
                    stop.set()
                    return
                _submit(text)
        threading.Thread(target=_reader, name="terminal-input", daemon=True).start()

    deadline = (time.monotonic() + args.exit_after) if args.exit_after else None
    last_route = None
    try:
        while not stop.is_set():
            snapshot = controller.snapshot()
            for entry in snapshot.get("logs") or []:
                category = str(entry.get("category") or "")
                message = str(entry.get("message") or "")
                print(f"  [{category:<7}] {message}")
            route = getattr(agent, "_last_route", None)
            if isinstance(route, dict) and route != last_route:
                last_route = dict(route)
                print(f"  [ROUTE  ] {route.get('device')} "
                      f"(score {route.get('score')}: "
                      f"{', '.join(route.get('signals') or []) or 'no signals'}) "
                      f"- {route.get('reason')}")
            if snapshot.get("error"):
                print(f"  [ERROR  ] {snapshot['error']}")
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        controller.stop()
    print(f"\nterminal closed: {speaker.delivered} repl(s) delivered")
    sys.stdout.flush()
    # The agent's own runtime starts the mesh listener and the model-host threads,
    # and they are not ours to join: a console that will not exit is worse than one
    # that skips atexit, and everything above this line has been flushed and was
    # already cleaned up by controller.stop(). Without this the process keeps the
    # mesh port bound and the next terminal run hangs waiting for it.
    os._exit(0)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.backend = backend_by_label(args.backend)["label"]
    explicit_url = args.url.strip()
    args.explicit_url = explicit_url
    args.url = explicit_url or backend_by_label(args.backend)["url"]
    if sys.stdout is None:      # pythonw.exe: any print() would raise
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()
    if hasattr(sys.stdout, "reconfigure"):
        # The agent's own model tracing prints model text; a cp1252 console
        # would raise UnicodeEncodeError inside the model call (see
        # android_inference.generate) and silently force rule fallback.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONUTF8", "1")
    if args.selftest:
        return selftest(args)
    if args.terminal:
        return run_terminal(args)
    if not TK_AVAILABLE:
        print("this Python has no tkinter, so the GUI cannot open; run with "
              "--terminal for the console engagement surface", file=sys.stderr)
        return 2
    controller = AgentController(interval=args.interval,
                                 sync_interval=args.sync_interval)
    ui = DesktopUI(controller, args)
    try:
        ui.mainloop()
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
