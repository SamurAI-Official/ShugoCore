#!/usr/bin/env python3
"""Loopback actuation sandbox (Phase D): prove what the agent may and may not do.

The hive's safety story is a chain of gates -- Tier 3 invariants, external
consent, human approval, a URL/method allowlist, a hash-bound policy token -- and
a chain of gates is only as good as its weakest refusal. This sandbox drives the
**real** pipeline (the engine's own ``_gate_decision`` then its own
``_execute_gated``, which mints the tamper-evident token and calls the real
``ExecutionLayer``) against a **real** HTTP service on 127.0.0.1 that records
exactly what actuation reached it.

So each scenario is checked twice: what the agent reported, and whether the bytes
arrived on the wire. A refusal that still hit the target would be a lie, and a
"success" that never landed would be theatre; both fail here.

    python3 actuation_sandbox.py --data-dir runtime/sandbox

Nothing leaves the machine: the target is loopback, the refused targets are
documentation addresses (RFC 5737), and the sandbox keeps its own memory db, audit
chain and logs inside ``--data-dir`` so a live node's chain is never touched.
"""
import argparse
import contextlib
import datetime
import http.server
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
import time

from policy import ApprovalBroker, CapabilityRegistry, ConsentRegistry
from security import canonical_hash

# An unroutable documentation address: if the allowlist ever failed to stop a LAN
# call, this scenario would break out of the sandbox by hanging instead of
# quietly passing -- the refusal is what proves nothing was attempted.
_LAN_TARGET = "https://192.0.2.1/actuate"
_INTERNET_TARGET = "https://api.example.com/v1/actuate"


def _tls_context(cert_dir: str = None):
    """A server TLS context plus its CA bundle, generated on the spot.

    ``cryptography`` ships with this fleet (it is a dependency of the memory
    stack), so the sandbox can present a *real* TLS endpoint and have the client
    trust it through ``REQUESTS_CA_BUNDLE`` -- rather than the far worse option of
    switching certificate verification off in a security test.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory = cert_dir or tempfile.mkdtemp(prefix="shugo_sandbox_tls_")
    os.makedirs(directory, exist_ok=True)
    now = datetime.datetime.utcnow()
    window = dict(not_valid_before=now - datetime.timedelta(minutes=5),
                  not_valid_after=now + datetime.timedelta(days=2))
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                            "shugo-sandbox-ca")])
    ca_cert = (x509.CertificateBuilder()
               .subject_name(ca_name).issuer_name(ca_name)
               .public_key(ca_key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(window["not_valid_before"])
               .not_valid_after(window["not_valid_after"])
               .add_extension(x509.BasicConstraints(ca=True, path_length=0),
                              critical=True)
               .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    leaf = (x509.CertificateBuilder()
            .subject_name(leaf_name).issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(window["not_valid_before"])
            .not_valid_after(window["not_valid_after"])
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.DNSName("localhost")]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_path = os.path.join(directory, "sandbox_ca.pem")
    server_path = os.path.join(directory, "sandbox_server.pem")
    with open(ca_path, "wb") as handle:
        handle.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    with open(server_path, "wb") as handle:
        handle.write(leaf.public_bytes(serialization.Encoding.PEM))
        handle.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(server_path)
    return context, ca_path


class _SandboxTLSServer(http.server.ThreadingHTTPServer):
    """An HTTPS server whose *accepted* connections are wrapped.

    Wrapping the listening socket instead leaves accepted sockets in plaintext,
    which fails exactly like a server-side handshake error.
    """

    def __init__(self, addr, handler, cert_dir=None):
        context, ca_path = _tls_context(cert_dir)
        self._context = context
        self.ca_path = ca_path
        super().__init__(addr, handler)

    def get_request(self):
        sock, addr = super().get_request()
        return self._context.wrap_socket(sock, server_side=True), addr


class LoopbackActuationTarget:
    """A real HTTP(S) service on 127.0.0.1 that records arriving actuation.

    Serves **TLS by default**, because the shipped egress policy allows only
    ``https`` (``security.validate_url``) -- so this is also a live check of that
    rule: the same scenario aimed at the plain-HTTP twin is refused. The
    certificate is generated for ``127.0.0.1`` and trusted via
    ``REQUESTS_CA_BUNDLE``, never by disabling verification.
    """

    def __init__(self, tls: bool = True, cert_dir: str = None):
        self.calls = []
        self.tls = bool(tls)
        self.ca_path = None
        self._lock = threading.Lock()
        sandbox = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _record(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except Exception:
                    body = raw.decode("utf-8", errors="replace")
                with sandbox._lock:
                    sandbox.calls.append({"method": method, "path": self.path,
                                          "body": body})
                    count = len(sandbox.calls)
                payload = json.dumps({"received": count, "path": self.path,
                                      "echo": body}).encode("utf-8")
                if self.path.startswith("/fail"):
                    self.send_response(500)
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._record("GET")

            def do_POST(self):
                self._record("POST")

            def do_DELETE(self):
                self._record("DELETE")

            def log_message(self, *args):        # keep the sandbox quiet
                pass

        if self.tls:
            self._server = _SandboxTLSServer(("127.0.0.1", 0), Handler, cert_dir)
            self.ca_path = self._server.ca_path
        else:
            self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.port = int(self._server.server_address[1])

    @property
    def url(self) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://127.0.0.1:{self.port}/actuate"

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)

    def last(self):
        with self._lock:
            return dict(self.calls[-1]) if self.calls else None

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass


@contextlib.contextmanager
def sandbox_workspace(path: str):
    """Run inside ``path`` -- the engine's Chroma store is cwd-relative.

    The host launcher and the Android agent both run with the data dir as their
    working directory, so the sandbox does the same instead of scattering a
    ``chroma_db`` into whatever directory it was invoked from.
    """
    os.makedirs(path, exist_ok=True)
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(previous)


def build_engine(name: str, *, api_hosts=None, allowed_methods=None,
                 consents=(), approval_ttl=2.0, operator=None):
    """A real ``DecisionEngine`` wired like a node's, for one scenario.

    Its memory db and audit chain live in the current directory (the sandbox
    workspace), under a per-scenario name so no two scenarios share SQLite or
    chain state.
    """
    from decision_engine import DecisionEngine

    engine = DecisionEngine(
        models=[],
        vector_db_config={"type": "chroma"},
        memory_db_path=f"sandbox_{name}.db",
        audit_path=f"sandbox_{name}.jsonl",
        capabilities=CapabilityRegistry({
            "api_hosts": list(api_hosts if api_hosts is not None
                              else ["api.duckduckgo.com", "newsapi.org"]),
            "allowed_methods": dict(allowed_methods or {}),
        }),
        consents=ConsentRegistry(),
        approvals=ApprovalBroker(ttl_seconds=approval_ttl),
    )
    for action_type, ttl in consents:
        engine.consents.grant(action_type, granted_by="actuation-sandbox",
                              ttl_seconds=ttl)
    if operator is not None:
        engine.approvals.attach_operator(operator)
    return engine


def gated_actuation(engine, decision: dict) -> dict:
    """Gate, then execute -- through the engine's own two steps.

    ``_gate_decision`` runs the Tier 3 invariants, the SAFE_STATE check, external
    consent and human approval; ``_execute_gated`` mints the hash-bound policy
    token before calling the real ``ExecutionLayer``. Calling these instead of a
    private copy is the point: the sandbox can only observe refusals the engine
    itself produces.
    """
    allowed, reason = engine._gate_decision(decision)
    if not allowed:
        return {"status": "refused", "reason": reason}
    return engine._execute_gated(decision)


def forged_token_actuation(engine, allowed_decision: dict,
                           tampered_decision: dict) -> dict:
    """Mint a verdict for one decision and replay it on different content.

    Mirrors ``_execute_gated``'s token shape on purpose: the negative case has to
    present a *plausible* verdict, and the layer must still reject it because the
    content hash no longer matches.
    """
    payload = dict(allowed_decision)
    payload["_trace"] = {"execution_id": "exe-sandbox-forged"}
    token = {"verdict": "allow", "decision_hash": canonical_hash(payload)}
    tampered = dict(tampered_decision)
    tampered["_policy"] = token
    return engine.execution_layer.execute(tampered)


def _api_call(url: str, method: str = "POST") -> dict:
    return {"action_type": "api_call", "confidence": 0.9, "decision_id": "dec-x",
            "params": {"url": url, "method": method,
                       "payload": {"probe": "actuation-sandbox"}}}


def _approve(_request) -> bool:
    return True


def _deny(_request) -> bool:
    return False


def _scenarios() -> list:
    """Every scenario: what it configures, what it expects, and why.

    ``expect`` is "allowed" or "refused"; ``delta`` is how many actuations may
    reach the loopback target; ``reason`` is a substring that must appear in a
    refusal, quoted from the gate's own wording so the check is about the *right*
    refusal and not merely about failing.
    """
    # The shipped method allowlist for api_call is GET only, so an actuation that
    # POSTs anything needs the operator to say so explicitly -- a property worth
    # keeping visible, which is why the method scenario below stays GET-only.
    ok_engine = {"api_hosts": ["127.0.0.1"],
                 "allowed_methods": {"api_call": ["GET", "POST"]},
                 "consents": [("api_call", None)], "operator": _approve}

    def loopback(targets):
        return _api_call(targets["tls"].url)

    return [
        {"name": "loopback_refused_by_default_allowlist",
         "why": "the shipped allowlist is two API hosts; loopback is not one",
         "engine": {"consents": [("api_call", None)], "operator": _approve},
         "decision": loopback, "expect": "refused", "delta": 0},
        {"name": "loopback_allowlisted_actuates",
         "why": "operator allowlist + consent + approval: the only scenario that "
                "may touch the wire",
         "engine": ok_engine, "decision": loopback, "expect": "allowed",
         "delta": 1},
        {"name": "loopback_plain_http_refused_by_scheme",
         "why": "the egress policy is https-only, so a local http service is "
                "unreachable even with consent and approval in hand",
         "engine": ok_engine,
         "decision": lambda targets: _api_call(targets["http"].url),
         "expect": "refused", "delta": 0,
         "reason": "URL scheme 'http' is not allowed"},
        {"name": "no_consent_refused",
         "why": "side-effecting action without an operator grant",
         "engine": {"api_hosts": ["127.0.0.1"]}, "decision": loopback,
         "expect": "refused", "delta": 0, "reason": "consent_required"},
        {"name": "expired_consent_refused",
         "why": "a grant that has aged out stops counting immediately",
         "engine": {"api_hosts": ["127.0.0.1"], "consents": [("api_call", 0.01)]},
         "before": lambda engine: time.sleep(0.05),
         "decision": loopback, "expect": "refused", "delta": 0,
         "reason": "consent_required"},
        {"name": "revoked_consent_refused",
         "why": "revocation takes effect without a restart",
         "engine": {"api_hosts": ["127.0.0.1"], "consents": [("api_call", None)]},
         "before": lambda engine: engine.consents.revoke("api_call"),
         "decision": loopback, "expect": "refused", "delta": 0,
         "reason": "consent_required"},
        {"name": "approval_denied_refused",
         "why": "the human gate is separate from consent, and can deny",
         "engine": {"api_hosts": ["127.0.0.1"], "consents": [("api_call", None)],
                    "operator": _deny},
         "decision": loopback, "expect": "refused", "delta": 0,
         "reason": "approval denied"},
        {"name": "approval_pending_refused",
         "why": "fail-closed when no operator answers in time",
         "engine": {"api_hosts": ["127.0.0.1"], "consents": [("api_call", None)],
                    "approval_ttl": 0.05},
         "decision": loopback, "expect": "refused", "delta": 0},
        {"name": "lan_target_refused",
         "why": "consent does not widen the host allowlist (RFC 5737 address)",
         "engine": ok_engine, "decision": lambda targets: _api_call(_LAN_TARGET),
         "expect": "refused", "delta": 0, "reason": "192.0.2.1"},
        {"name": "internet_target_refused",
         "why": "consent does not widen the host allowlist (public host)",
         "engine": ok_engine,
         "decision": lambda targets: _api_call(_INTERNET_TARGET),
         "expect": "refused", "delta": 0, "reason": "api.example.com"},
        {"name": "method_not_allowed_refused",
         "why": "the method allowlist is enforced, not only the host",
         "engine": {"api_hosts": ["127.0.0.1"],
                    "allowed_methods": {"api_call": ["GET"]},
                    "consents": [("api_call", None)], "operator": _approve},
         "decision": lambda targets: _api_call(targets["tls"].url, method="DELETE"),
         "expect": "refused", "delta": 0, "reason": "method"},
        {"name": "forged_verdict_refused",
         "why": "the policy token is bound to the decision content",
         "engine": ok_engine, "mode": "forged", "decision": loopback,
         "expect": "refused", "delta": 0,
         "reason": "policy verdict does not match decision content"},
        {"name": "missing_verdict_refused",
         "why": "the layer refuses anything that skipped the gate",
         "engine": ok_engine, "mode": "raw", "decision": loopback,
         "expect": "refused", "delta": 0, "reason": "missing policy verdict"},
        {"name": "safe_state_refused",
         "why": "read-only mode blocks side effects even with a live grant",
         "engine": ok_engine, "mode": "safe_state", "decision": loopback,
         "expect": "refused", "delta": 0, "reason": "SAFE_STATE"},
    ]


def _audit_evidence(chain_path: str) -> dict:
    """What the node's own chain recorded for one scenario."""
    events = []
    try:
        with open(chain_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                events.append(str(entry.get("type") or ""))
    except OSError:
        return {"events": [], "executions": 0, "approvals": 0, "refusals": 0}
    return {"events": events,
            "executions": sum(1 for e in events if "execution" in e),
            "approvals": sum(1 for e in events if "approval" in e),
            "refusals": sum(1 for e in events if "refus" in e or "denied" in e)}


def _run_one(spec: dict, targets: dict, data_dir: str) -> dict:
    name = spec["name"]
    before = targets["tls"].count + targets["http"].count
    wire = lambda: targets["tls"].count + targets["http"].count      # noqa: E731
    entry = {"name": name, "why": spec.get("why", ""), "expect": spec["expect"],
             "delta_allowed": spec["delta"]}
    engine = build_engine(name, **spec.get("engine", {}))
    try:
        if spec.get("before"):
            spec["before"](engine)
        decision = spec["decision"](targets)
        mode = spec.get("mode", "gated")
        if mode == "raw":
            # Straight to the layer, the way a bug or a bypassed gate would.
            result = engine.execution_layer.execute(decision)
        elif mode == "safe_state":
            engine.governor.safe_state("actuation-sandbox")
            result = gated_actuation(engine, decision)
        elif mode == "forged":
            result = forged_token_actuation(engine, _api_call(targets["tls"].url),
                                            _api_call(_LAN_TARGET))
        else:
            result = gated_actuation(engine, decision)
    except Exception as exc:            # a raise here is itself a failure
        entry.update({"observed": "error", "delta": wire() - before,
                      "reason": f"{type(exc).__name__}: {exc}", "ok": False})
        return entry
    status = str(result.get("status") or "")
    if status == "success":
        observed = "allowed"
    elif status == "refused":
        observed = "refused"
    else:
        observed = status or "none"
    reason = str(result.get("reason") or result.get("message") or "")
    evidence = _audit_evidence(os.path.join(data_dir, f"sandbox_{name}.jsonl"))
    ok = observed == spec["expect"] and (wire() - before) == spec["delta"]
    if spec.get("reason"):
        ok = ok and spec["reason"] in reason
    entry.update({"observed": observed, "delta": wire() - before,
                  "reason": reason[:160],
                  "http_status": result.get("http_status"),
                  "chain_executions": evidence["executions"],
                  "chain_approvals": evidence["approvals"], "ok": ok})
    return entry


def mobile_topic_scenarios() -> list:
    """Device-side containment: commands never reach actuation topics.

    Two gates in series here: a device must be paired before its topics are read
    at all, and even a paired device may only publish on the contracted sensor
    namespace -- the actuation tail is unreachable by construction.
    """
    registry = CapabilityRegistry({"mobile_devices_allowlist": ["tab-s9fe"]})
    sensor_ok, _ = registry.validate_mobile_topic("tab-s9fe", "imu")
    actuation_ok, reason = registry.validate_mobile_topic("tab-s9fe",
                                                          "actuation/move")
    unpaired_ok, unpaired = registry.validate_mobile_topic("ghost", "imu")
    return [{"name": "mobile_actuation_topic_refused",
             "why": "a host command must not reach a device's actuation topics",
             "expect": "refused",
             "observed": "allowed" if actuation_ok else "refused",
             "reason": reason,
             "ok": (not actuation_ok) and "outside the mobile contract" in reason},
            {"name": "mobile_sensor_topic_allowed",
             "why": "the contracted sensor namespace stays reachable",
             "expect": "allowed",
             "observed": "allowed" if sensor_ok else "refused",
             "reason": "", "ok": bool(sensor_ok)},
            {"name": "unpaired_device_refused",
             "why": "topic access requires pairing first",
             "expect": "refused",
             "observed": "allowed" if unpaired_ok else "refused",
             "reason": unpaired,
             "ok": (not unpaired_ok) and "pairing allowlist" in unpaired}]


def run_scenarios(data_dir: str, only=None) -> list:
    """Run every scenario against a real loopback target (no other egress).

    The TLS target is trusted by pointing ``REQUESTS_CA_BUNDLE`` at the CA this
    run generated, and a plain-HTTP twin exists purely so a scenario can show
    that the https-only rule refuses it.
    """
    # Absolute up front: the workspace below changes the process cwd, and the CA
    # bundle path handed to requests (and the chain evidence read back) would
    # otherwise resolve against the wrong directory.
    data_dir = os.path.abspath(data_dir)
    tls_target = LoopbackActuationTarget(tls=True, cert_dir=data_dir)
    http_target = LoopbackActuationTarget(tls=False)
    previous_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
    if tls_target.ca_path:
        os.environ["REQUESTS_CA_BUNDLE"] = tls_target.ca_path
    rows = []
    try:
        with sandbox_workspace(data_dir):
            for spec in _scenarios():
                if only and spec["name"] not in only:
                    continue
                rows.append(_run_one(spec, {"tls": tls_target, "http": http_target},
                                     data_dir))
        rows.extend(mobile_topic_scenarios())
    finally:
        if previous_bundle is None:
            os.environ.pop("REQUESTS_CA_BUNDLE", None)
        else:
            os.environ["REQUESTS_CA_BUNDLE"] = previous_bundle
        tls_target.stop()
        http_target.stop()
    return rows


def render(rows) -> str:
    width = max(len(row["name"]) for row in rows) if rows else 10
    head = (f"{'scenario'.ljust(width)} | {'expect':<8} | {'observed':<10} | "
            f"{'wire':<4} | {'chain':<5} | ok")
    lines = [head, "-" * len(head)]
    for row in rows:
        wire = row.get("delta", row.get("wire", ""))
        chain = row.get("chain_executions", "")
        lines.append(f"{row['name'].ljust(width)} | {row['expect']:<8} | "
                     f"{row['observed']:<10} | {str(wire):<4} | "
                     f"{str(chain):<5} | {'PASS' if row['ok'] else 'FAIL'}")
    failed = [row for row in rows if not row["ok"]]
    lines.append("")
    lines.append(f"{len(rows) - len(failed)}/{len(rows)} scenarios behaved as "
                 f"required")
    for row in failed:
        lines.append(f"  FAIL {row['name']}: expected {row['expect']} "
                     f"(wire delta {row.get('delta_allowed')}), observed "
                     f"{row['observed']} (wire {row.get('delta')}) "
                     f"{row.get('reason') or ''}".rstrip())
    return "\n".join(lines)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Loopback actuation sandbox: prove what the agent may actuate")
    ap.add_argument("--data-dir", default="runtime/sandbox",
                    help="where the sandbox keeps its own db/chain/logs "
                         "(default: runtime/sandbox)")
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="run just this scenario (repeatable)")
    ap.add_argument("--json", action="store_true", help="also dump raw rows")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    rows = run_scenarios(args.data_dir, only=set(args.only) or None)
    print(render(rows))
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())


