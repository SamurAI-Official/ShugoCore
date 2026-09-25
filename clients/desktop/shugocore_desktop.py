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
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import ttk

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

    def __init__(self, interval: float = 2.0):
        self.interval = max(0.5, float(interval))
        self.agent = None
        self._thread = None
        self._stop = threading.Event()
        self.started_at = 0.0
        self.last_error = ""
        self.log_seq = 0
        self._logs = []
        self.applied = {}

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
        while not self._stop.is_set():
            agent = self.agent
            if agent is None:
                time.sleep(0.2)
                continue
            try:
                agent.tick()
            except Exception as exc:                  # keep cycling, surface it
                self.last_error = f"tick failed: {exc}"
            self._stop.wait(self.interval)

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
                "uptime": (round(time.time() - self.started_at, 1)
                           if self.started_at else 0.0)}


class DesktopUI(tk.Tk):
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
        mesh = f"{status.get('mesh_role') or 'none'}"
        if status.get("mesh_primary"):
            mesh += f" (primary {status['mesh_primary']})"
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
                  text="A desktop node has no camera/IMU/GPS of its own: "
                       "sensors live on the Android nodes and arrive over the "
                       "mesh. The rows below report only what this node "
                       "actually declared or received - nothing is invented."
                  ).pack(anchor="w")
        rows = self._section(parent, "Declared capabilities")
        self.sensor_rows = {}
        for index, (key, label) in enumerate((("declared", "Declared"),
                                              ("telemetry", "Telemetry seen"),
                                              ("peers", "Mesh peers"))):
            self._kv(rows, label, self.sensor_rows, key, index)

    def _update_sensors(self, snap: dict) -> None:
        status = snap["status"]
        caps = status.get("capabilities") or {}
        declared = [key for key, value in caps.items()
                    if isinstance(value, dict)
                    and (value.get("granted") or value.get("declared"))]
        self.sensor_rows["declared"].config(
            text=", ".join(declared[:10]) or "none declared")
        self.sensor_rows["telemetry"].config(
            text=str(bool(status.get("telemetry_received"))))
        peers = status.get("mesh_peers") or []
        names = [str(p.get("device_id", p.get("id", p)))
                 if isinstance(p, dict) else str(p) for p in peers[:8]]
        self.sensor_rows["peers"].config(text=", ".join(names) or "none")

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
    parser.add_argument("--autostart", action="store_true",
                        help="start the node immediately on launch")
    parser.add_argument("--selftest", action="store_true",
                        help="probe the backends and exit (no GUI)")
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
    controller = AgentController(interval=args.interval)
    ui = DesktopUI(controller, args)
    try:
        ui.mainloop()
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
