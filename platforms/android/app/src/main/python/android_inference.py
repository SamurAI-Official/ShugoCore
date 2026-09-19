"""
ShugoCore Android Inference Backend

Provides OpenAI-compatible API client for the local llama.cpp
server running via JNI bindings.

This allows ShugoCore's existing backends (OllamaBackend, OpenAIBackend)
to work unchanged on Android through the local API server.
"""

import json
import logging
import os
import shutil
import subprocess
from urllib.request import urlopen, Request
from urllib.error import URLError
from typing import Optional, Dict, Any, List
from model_backends import BaseBackend, register_backend


def _http_post(url: str, payload: dict, timeout: float,
               auth_token: Optional[str] = None) -> dict:
    """POST JSON via stdlib urllib (``requests`` is not bundled in Chaquopy).

    When ``auth_token`` is supplied, it's sent as ``Authorization: Bearer <token>``
    so the same HTTP helper can talk to token-protected desktop servers as
    well as the bare local llama.cpp endpoint. The helper is local to this
    module and never logs the header.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    req = Request(url, data=data, headers=headers)
    with urlopen(req, timeout=int(timeout)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(url: str, timeout: float,
              auth_token: Optional[str] = None) -> dict:
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if headers:
        req = Request(url, headers=headers)
    else:
        req = url
    with urlopen(req if isinstance(req, str) else req,  # type: ignore[arg-type]
                 timeout=int(timeout)) as resp:
        return json.loads(resp.read().decode("utf-8"))


class AndroidBackend(BaseBackend):
    """Model backend for Android local inference server.

    Connects to LocalApiServer running llama.cpp via JNI.
    Compatible with OllamaBackend interface (same API format).

    ``auth_token`` (v1.30.4): optional bearer token sent on every request
    so the same client can also reach a token-protected *desktop* server
    (the SHUGOCORE_SERVER_TOKEN guard on ``shugocore-server``). The token
    is never logged or echoed back.
    """

    name = "android"

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:11434",
        model_name: str = "shugocore",
        device_caps: Optional[Dict[str, Any]] = None,
        # v1.28.2: accept base_url as an alias (delegation manager sets
        # base_url; OllamaBackend uses base_url, AndroidBackend uses
        # api_url -- explicit wins, alias fills when explicit is default).
        base_url: Optional[str] = None,
        # v1.30.4: optional bearer token to send on every request.
        auth_token: Optional[str] = None,
        # CPU-only on-device generation needs ample headroom for prompt
        # eval (prefill) + decode. On a 0.5B Q4 model on Exynos 1380:
        # prefill ~280s + decode ~1.3s/token. Full 80-token generation
        # needs ~385s; 600s matches the governor deadline and absorbs
        # thermal-throttle slowdowns.
        timeout: int = 600,
    ):
        if base_url and api_url == "http://127.0.0.1:11434":
            api_url = base_url
        self.base_url = api_url.rstrip("/")
        self.model_name = model_name
        self.device_caps = device_caps or {}
        self.timeout = timeout
        # Strip + sanitize the token; an empty string is the same as None.
        token = str(auth_token).strip() if auth_token else ""
        self.auth_token: Optional[str] = token or None

    def generate(
        self,
        model_id: str,
        prompt: str,
        timeout: float = 30.0,
        grammar: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Generate text using the local llama.cpp server.

        ``grammar`` is an optional GBNF string; LocalApiServer forwards it to
        llama.cpp's grammar sampler so structured callers get JSON the model
        physically cannot deviate from (instead of best-effort prose).
        """
        payload = {
            "model": model_id,
            "prompt": prompt,
            "stream": False,
            "options": {
                # Structured decision JSON needs room for preamble + the JSON
                # object. On the tiny 0.5B model the preamble often consumes
                # tokens; capping too low truncates mid-JSON (parse fails ->
                # no_viable_action). No newline stop: single-line JSON is
                # preferred but a preamble + newline must NOT halt generation
                # before the JSON appears.
                "num_predict": kwargs.get("max_tokens", 128),
                "temperature": kwargs.get("temperature", 0.7),
                "top_p": kwargs.get("top_p", 0.9),
                "top_k": kwargs.get("top_k", 40),
            },
        }
        if grammar:
            payload["grammar"] = grammar
        print(f"ANDROID_INFERENCE generate called: model_id={model_id} "
              f"prompt_len={len(prompt)} grammar={'on' if grammar else 'off'}")
        data = _http_post(
            f"{self.base_url}/api/generate", payload, timeout or self.timeout,
            auth_token=self.auth_token,
        )
        response_text = data.get("response", "")
        if not response_text:
            logging.getLogger(__name__).warning("Empty response from model")
        print(f"MODEL_RESPONSE_text_len={len(response_text)} text={response_text!r}")
        return response_text

    def chat(
        self,
        model_id: str,
        messages: List[Dict[str, str]],
        timeout: float = 30.0,
        grammar: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Chat completion using the local llama.cpp server."""
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": kwargs.get("max_tokens", 128),
                "temperature": kwargs.get("temperature", 0.7),
                "top_p": kwargs.get("top_p", 0.9),
            },
        }
        if grammar:
            payload["grammar"] = grammar
        data = _http_post(
            f"{self.base_url}/api/chat", payload, timeout or self.timeout,
            auth_token=self.auth_token,
        )
        return data.get("message", {}).get("content", "")

    def list_models(self) -> List[str]:
        """List available models via the /api/tags endpoint."""
        try:
            data = _http_get(f"{self.base_url}/api/tags", self.timeout,
                             auth_token=self.auth_token)
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return [self.model_name]

    def get_health(self) -> bool:
        """Check if the local inference server is running."""
        try:
            _http_get(f"{self.base_url}/health", 5,
                      auth_token=self.auth_token)
            return True
        except Exception:
            return False


# Self-register so ``create_backend({"type": "android", ...})`` works.
register_backend("android", AndroidBackend)


# ---------------------------------------------------------------------------
# Termux llama-server launcher (v1.30.4)
# ---------------------------------------------------------------------------
# The Chaquopy app path embeds llama.cpp via JNI. A Termux deployment (the
# README "Android llama.cpp-compatible host server for Termux" roadmap item)
# instead shells OUT to the distribution's own llama-server binary and talks
# to it over loopback HTTP — the agent works against a pkg-installed
# llama.cpp with zero native JNI code in the Python layer.
def find_llama_server(prefix: Optional[str] = None,
                      which: Optional[Any] = None) -> Optional[str]:
    """Locate a llama-server binary for the current environment.

    Search order:
      1. ``$PREFIX/bin/llama-server`` — Termux package install location.
      2. ``shutil.which("llama-server")`` on PATH (also covers adb shell).
      3. ``SHUGOCORE_LLAMA_SERVER`` env override (explicit operator path).

    Returns None when no binary is found (the caller falls back to the
    deterministic stub / Chaquopy JNI path unchanged).
    """
    if which is None:
        which = shutil.which
    env_override = (os.environ.get("SHUGOCORE_LLAMA_SERVER", "") or "").strip()
    if env_override:
        return env_override
    prefix = os.environ.get("PREFIX", "") if prefix is None else str(prefix)
    if prefix:
        candidate = os.path.join(prefix, "bin", "llama-server")
        try:
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            pass
    try:
        found = which("llama-server")
        if found:
            return str(found)
    except Exception:
        pass
    return None


_POPEN = subprocess.Popen


class TermuxLlamaServer:
    """Owns one llama-server child process for a Termux deployment.

    ``start()`` refuses to run when no binary or no model is available
    (fail-closed — a silently-dead server would make every model call
    error). ``stop()`` terminates the child (best-effort, idempotent).
    ``running()`` reports process liveness without touching the network.
    """

    def __init__(self, model_path: str,
                 binary: Optional[str] = None,
                 host: str = "127.0.0.1",
                 port: int = 8080,
                 extra_args: Optional[List[str]] = None,
                 popen: Optional[Any] = None,
                 logger: Optional[Any] = None):
        self.model_path = str(model_path or "").strip()
        self.binary = binary or find_llama_server()
        self.host = str(host or "127.0.0.1")
        self.port = max(1, min(65535, int(port)))
        self.extra_args = list(extra_args or [])
        self._popen = popen or _POPEN
        self._log = logger or logging.getLogger(__name__)
        self._proc: Optional[Any] = None

    def start(self) -> bool:
        """Launch llama-server against the model; True when spawned."""
        if not self.model_path:
            self._log.warning("TermuxLlamaServer: no model path to serve")
            return False
        if not self.binary:
            self._log.warning(
                "TermuxLlamaServer: no llama-server binary (set "
                "SHUGOCORE_LLAMA_SERVER or install the termux package)")
            return False
        cmd = [self.binary,
               "--model", self.model_path,
               "--host", self.host,
               "--port", str(self.port)]
        cmd.extend(self.extra_args)
        try:
            self._proc = self._popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self._log.error("TermuxLlamaServer: spawn failed: %s", exc)
            self._proc = None
            return False
        self._log.info("TermuxLlamaServer: spawned %s pid=%s on %s:%s",
                       self.binary, getattr(self._proc, "pid", "?"),
                       self.host, self.port)
        return True

    def running(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            return False

    def stop(self, timeout: float = 3.0) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=float(timeout))
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._proc = None

