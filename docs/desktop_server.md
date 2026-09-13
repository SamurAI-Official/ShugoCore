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
# Binding to a non-loopback address is an explicit decision (see
# "Authentication & exposure" below). The bundled Android app does not send
# an auth token yet, so LAN pairing uses the acknowledgement flag:
shugocore-server --backend ollama --model qwen3.5:latest \
    --host 0.0.0.0 --allow-unauthenticated
```

Then in the Android app, enter `http://<desktop-ip>:<port>` in the
"Desktop server URL" field and press **Start Agent**.

> If a local Ollama already owns port `11434`, pick another port:
> `shugocore-server --port 11435` and enter `http://<desktop-ip>:11435`.

## Authentication & exposure

The server is **loopback by default** (`--host 127.0.0.1`). Binding to a
non-loopback address is **refused** unless you either:

* set `SHUGOCORE_SERVER_TOKEN` — every route except `/health` then requires
  `Authorization: Bearer <token>` (or `X-ShugoCore-Token: <token>`); or
* pass `--allow-unauthenticated` — an explicit acknowledgement that the
  model and engine endpoints are reachable by anyone who can reach the port.

```bash
# Token-protected (API clients that can send an Authorization header):
export SHUGOCORE_SERVER_TOKEN="$(openssl rand -hex 32)"
shugocore-server --backend ollama --host 0.0.0.0

# Android app pairing today (no client token support yet) — keep the port on
# a trusted network / behind a firewall:
shugocore-server --backend ollama --host 0.0.0.0 --allow-unauthenticated
```

Additional abuse controls, always on:

* Per-client token-bucket rate limiting (`--rate-limit-per-minute`, default
  240; `--rate-limit-burst`, default 120). `/health` is exempt.
* Request bodies are capped at 1 MB and a negative/oversized
  `Content-Length` is rejected without reading.
* CORS preflight is answered only for loopback origins.

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
shugocore-server --backend ollama --model qwen3.5:latest \
    --host 0.0.0.0 --allow-unauthenticated
```

Allow incoming connections on the chosen port when macOS prompts.

### Linux

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:latest
pip install shugocore
shugocore-server --backend ollama --model qwen3.5:latest \
    --host 0.0.0.0 --allow-unauthenticated
```

For a systemd unit:

```ini
[Unit]
Description=ShugoCore desktop server
After=network.target ollama.service

[Service]
# Option A: token-protected (clients must send the bearer token)
#EnvironmentFile=/etc/shugocore.env   # SHUGOCORE_SERVER_TOKEN=...
#ExecStart=/usr/local/bin/shugocore-server --backend ollama --model qwen3.5:latest --host 0.0.0.0
# Option B: explicit unauthenticated LAN exposure (Android app pairing today)
ExecStart=/usr/local/bin/shugocore-server --backend ollama --model qwen3.5:latest --host 0.0.0.0 --allow-unauthenticated
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
shugocore-server --backend ollama --model qwen3.5:latest \
    --host 0.0.0.0 --allow-unauthenticated
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

When `SHUGOCORE_SERVER_TOKEN` is set, add the header to every route except
`/health`:

```bash
curl -H "Authorization: Bearer $SHUGOCORE_SERVER_TOKEN" \
  http://127.0.0.1:11434/api/v1/status
```

The phone's own `AndroidBackend` uses these same endpoints, so pairing is
"just point the URL field at the desktop".