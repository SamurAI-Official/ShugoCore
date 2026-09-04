# ShugoCore Desktop Server

For users without a 2020+ mid/high-tier Android phone, the desktop can run the
full ShugoCore agent and the phone pairs to it as a lightweight sensor/operator
node.

The desktop server (`shugocore-server`) is a **stdlib-only** HTTP server that
speaks the same Ollama wire contract the Android app already uses, plus the
engine API for policy-gated task execution.

```
Phone (AndroidBackend)
   │  http://<desktop-ip>:<port>
   ▼
shugocore-server ──┬── /api/generate  ── backend ──┬─ Ollama (recommended)
                   │  /api/chat                     ├─ llama.cpp llama-server
                   │  /api/tags                     ├─ any OpenAI-compatible API
                   │  /health                       └─ stub (offline tests)
                   │
                   └── /api/v1/status   (engine/governor/memory state)
                       POST /api/v1/task (policy-gated execute_task path)
```

## Quick start (all three OSes)

```bash
pip install shugocore
shugocore-server --backend ollama --model qwen3.5:latest --host 0.0.0.0
```

Then in the Android app, enter `http://<desktop-ip>:<port>` in the
"Desktop server URL" field and press **Start Agent**.

> If a local Ollama already owns port `11434`, pick another port:
> `shugocore-server --port 11435` and enter `http://<desktop-ip>:11435`.

## Backends

| `--backend` | Base URL (default) | Notes |
|---|---|---|
| `ollama` | `http://127.0.0.1:11434` | Recommended. Ollama is available on all 3 OSes. |
| `llamacpp` | `http://127.0.0.1:8080` | Points at a llama.cpp `llama-server` (OpenAI-compatible). |
| `openai` | `https://api.openai.com/v1` | Any OpenAI-compatible chat endpoint. |
| `stub` | — | Deterministic offline stub for tests / dry runs. |

Set a custom URL per backend with `--backend-url`.

## Engine API

```
GET  /api/v1/status     engine + governor + fallback + memory snapshot
POST /api/v1/task       {"type": "text", "content": "...", "params": {...}}
```

`POST /api/v1/task` runs through `DecisionEngine.execute_task` — the same
governor interlocks, policy checks, approval broker and audit chain as a local
call. Harmful/invariant-violating tasks are refused; side-effecting actions
require consent + approval as usual. Request bodies are capped at 1 MB.

## Per-OS setup

### macOS

```bash
brew install ollama
ollama serve &        # or: brew services start ollama
ollama pull qwen3.5:latest
pip install shugocore
shugocore-server --backend ollama --model qwen3.5:latest --host 0.0.0.0
```

Allow incoming connections on the chosen port when macOS prompts.

### Linux

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:latest
pip install shugocore
shugocore-server --backend ollama --model qwen3.5:latest --host 0.0.0.0
```

For a systemd unit:

```ini
[Unit]
Description=ShugoCore desktop server
After=network.target ollama.service

[Service]
ExecStart=/usr/local/bin/shugocore-server --backend ollama --model qwen3.5:latest --host 0.0.0.0
Restart=on-failure
User=<your-user>

[Install]
WantedBy=multi-user.target
```

Firewall: allow the server port (default 11434/11435):
`sudo ufw allow 11434/tcp`.

### Windows

```powershell
# Install Ollama from https://ollama.com/download/windows
ollama pull qwen3.5:latest
pip install shugocore
shugocore-server --backend ollama --model qwen3.5:latest --host 0.0.0.0
```

Add an inbound firewall rule for the port:

```powershell
New-NetFirewallRule -DisplayName "ShugoCore" -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow
```

## Testing by hand

```bash
curl http://127.0.0.1:11434/health
curl -X POST http://127.0.0.1:11434/api/generate \
  -d '{"model":"qwen3.5:latest","prompt":"say hello","stream":false}'
curl http://127.0.0.1:11434/api/v1/status
curl -X POST http://127.0.0.1:11434/api/v1/task \
  -d '{"type":"text","content":"explain the plan"}'
```

The phone's own `AndroidBackend` uses these same endpoints, so pairing is
"just point the URL field at the desktop".