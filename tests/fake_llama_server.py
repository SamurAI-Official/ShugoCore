"""Fake llama.cpp / Ollama / LM Studio host server (test double).

A real ThreadingHTTPServer on loopback implementing the exact wire protocols
ShugoCore probes/calls, so model-execution stress tests exercise the real
requests + detect_local_launcher + backend code paths — no native deps.

Endpoints: GET /health, GET /v1/models, GET /api/tags,
POST /v1/chat/completions, POST /api/generate.

Failure modes: mode=status(status=N), mode=hang(latency=N),
mode=malformed, mode=empty_choices.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class FakeLlamaServer:
    def __init__(self, host="127.0.0.1", port=0, mode="ok", status=500,
                 latency=0.0, hang_seconds=5.0,
                 response_text="fake-model-response", model_name="fake-model"):
        self.host = host
        self.port = port
        self.mode = mode
        self.status = status
        self.latency = latency
        self.hang_seconds = hang_seconds
        self.response_text = response_text
        self.model_name = model_name
        self._server = None
        self._thread = None
        self.requests = []
        self._lock = threading.Lock()

    def start(self):
        self._server = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def request_count(self):
        with self._lock:
            return len(self.requests)

    def _record(self, method, path, body):
        with self._lock:
            self.requests.append({"method": method, "path": path, "body": body})

    def _make_handler(self):
        server = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _respond(self, payload, status=200):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_body(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw)
                except (TypeError, ValueError):
                    return {}

            def _handle(self, method, path):
                body = self._read_body() if method == "POST" else {}
                server._record(method, path, body)

                if server.mode == "hang":
                    time.sleep(server.hang_seconds)
                    self._respond({}, 200)
                    return
                if server.latency > 0:
                    time.sleep(server.latency)
                if server.mode == "status":
                    self._respond({"error": "forced failure"}, server.status)
                    return
                if server.mode == "malformed":
                    self._respond_raw(b"not json", 200)
                    return

                if path == "/health":
                    self._respond({"status": "ok"})
                elif path == "/v1/models":
                    self._respond({"object": "list", "data": [
                        {"id": server.model_name, "object": "model"}]})
                elif path == "/api/tags":
                    self._respond({"models": [{"name": server.model_name}]})
                elif path == "/v1/chat/completions":
                    if server.mode == "empty_choices":
                        self._respond({"choices": []})
                        return
                    self._respond({
                        "id": "chatcmpl-fake", "object": "chat.completion",
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant",
                                        "content": server.response_text},
                            "finish_reason": "stop"}]})
                elif path == "/api/generate":
                    if server.mode == "empty_choices":
                        self._respond({"response": ""})
                        return
                    self._respond({"model": server.model_name,
                                  "response": server.response_text,
                                  "done": True})
                else:
                    self._respond({"error": f"unknown path {path}"}, 404)

            def _respond_raw(self, data, status=200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET", self.path)

            def do_POST(self):
                self._handle("POST", self.path)

        return H


def start_fake_server(**kwargs):
    return FakeLlamaServer(**kwargs).start()
