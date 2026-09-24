"""
ShugoCore netwatch tests
========================

Tests for ``scripts/netwatch.py`` -- the standalone link-cycle timeline
recorder documented in the README ("Link-cycle correlation").

The module is loaded from its path on purpose: ``scripts/`` holds operator
tooling rather than an installed package, and netwatch has to stay importable
when the repo root is *not* on ``sys.path`` (it runs on a bare node).

Everything here is deterministic: ``FakeProbe`` replaces the network with a
scripted frame per tick and an injected clock, so no test depends on a real
link, on DNS, or on wall-clock time.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_NETWATCH_PATH = _REPO_ROOT / "scripts" / "netwatch.py"


def _load_netwatch():
    """Load ``scripts/netwatch.py`` without requiring it to be importable."""
    spec = importlib.util.spec_from_file_location("netwatch_under_test", _NETWATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["netwatch_under_test"] = module
    spec.loader.exec_module(module)
    return module


netwatch = _load_netwatch()

try:  # the severity netwatch emits must stay in sync with the fallback table
    from shugonet_bridge import NETWORK_FALLBACK_SEVERITIES
except ImportError:  # pragma: no cover - shugonet_bridge is in the repo
    NETWORK_FALLBACK_SEVERITIES = {}

# Endpoints the fake frames answer for; they match the watcher settings below.
TARGET = "127.0.0.1:11435"
LAN_TARGET = "192.168.1.162:11435"
GATEWAY = "192.168.1.1"
GATEWAY_PORT = 80

WATCHER_KWARGS = {
    "dns_name": "swscan.apple.com",
    "target": TARGET,
    "internet": "9.9.9.9:443,1.1.1.1:443",
    "gateway_port": GATEWAY_PORT,
}

OPEN = netwatch.CONNECT_OPEN
REFUSED = netwatch.CONNECT_REFUSED
UNREACHABLE = netwatch.CONNECT_UNREACHABLE

DEFAULT_CONNECT = {
    TARGET: {"result": REFUSED, "ms": 0.2},
    LAN_TARGET: {"result": REFUSED, "ms": 0.5},
    "9.9.9.9:443": {"result": OPEN, "ms": 12.0},
    "1.1.1.1:443": {"result": REFUSED, "ms": 40.0},
    "192.168.1.1:80": {"result": OPEN, "ms": 3.0},
}


def frame(connect=None, **overrides):
    """One scripted tick: healthy by default, merged with ``connect``/overrides.

    ``connect`` maps ``"host:port"`` to a result dict; anything the frame does
    not mention is reported as unreachable.
    """
    data = {
        "local_addr": "192.168.1.162",
        "gateway": GATEWAY,
        "dns_ms": 5.0,
        "connect": dict(DEFAULT_CONNECT),
    }
    if connect:
        data["connect"].update(connect)
    data.update(overrides)
    return data


class FakeProbe:
    """Deterministic stand-in for ``netwatch.NetProbe``.

    Every tick starts with a gateway lookup (see ``sample()``), so that is
    where the next scripted frame is selected; ``load()`` pins a single frame
    for tests that call ``tick()`` directly.
    """

    def __init__(self, frames=None, step=0.01, timeout=1.5):
        self.frames = list(frames or [])
        self.step = float(step)
        self.timeout = timeout
        self.clock = 0.0
        self.tick_index = 0
        self.sleeps = []
        self.resolved = []
        self.wall = datetime(2026, 9, 19, 15, 0, 0, tzinfo=timezone.utc)
        self.frame = frame()

    # -- test control ------------------------------------------------------
    def load(self, data):
        self.frames = []
        self.frame = dict(data)
        return self

    def _select_frame(self):
        if self.frames:
            index = min(self.tick_index, len(self.frames) - 1)
            self.frame = dict(self.frames[index])
        self.tick_index += 1
        return self.frame

    # -- netwatch.NetProbe interface ---------------------------------------
    def now(self):
        self.clock += self.step
        return self.clock

    def sleep(self, seconds):
        self.sleeps.append(float(seconds))
        self.clock += float(seconds)

    def wall_now(self):
        return self.wall

    def local_address(self):
        return self.frame.get("local_addr")

    def default_gateway(self):
        self._select_frame()
        return self.frame.get("gateway")

    def resolve_ms(self, name):
        self.resolved.append(name)
        return self.frame.get("dns_ms")

    def connect(self, host, port):
        key = "%s:%d" % (host, int(port))
        answer = (self.frame.get("connect") or {}).get(key)
        if answer is None:
            return {"result": UNREACHABLE, "ms": None}
        return dict(answer)


def make_watcher(probe):
    """Watcher wired to the deterministic endpoints above."""
    return netwatch.NetWatcher(probe=probe, **WATCHER_KWARGS)


def make_factory(frames):
    """``probe_factory`` for ``main()`` that walks ``frames`` one per tick."""
    def factory(timeout=1.5):
        return FakeProbe(frames=list(frames), timeout=timeout)
    return factory


def records_of(records, record_type):
    """Filter emitted records by type."""
    return [record for record in records if record["type"] == record_type]


class TestParseEndpoint(unittest.TestCase):
    """Endpoint parsing feeds every probe, so it must be boringly predictable."""

    def test_host_and_port(self):
        self.assertEqual(netwatch.parse_endpoint("192.168.1.10:11435"), ("192.168.1.10", 11435))

    def test_host_only_uses_default_port(self):
        self.assertEqual(netwatch.parse_endpoint("apple.com", 443), ("apple.com", 443))

    def test_port_may_be_empty(self):
        self.assertEqual(netwatch.parse_endpoint("apple.com:", 8443), ("apple.com", 8443))

    def test_bracketed_ipv6_is_supported(self):
        self.assertEqual(netwatch.parse_endpoint("[fe80::1]:8080"), ("fe80::1", 8080))

    def test_bare_ipv6_literal_uses_default_port(self):
        parsed = netwatch.parse_endpoint("2001:4860:4860::8888")
        self.assertEqual(parsed, ("2001:4860:4860::8888", 443))

    def test_empty_endpoint_raises(self):
        with self.assertRaises(ValueError):
            netwatch.parse_endpoint("   ")

    def test_missing_host_raises(self):
        with self.assertRaises(ValueError):
            netwatch.parse_endpoint(":443")

    def test_non_numeric_port_raises(self):
        with self.assertRaises(ValueError):
            netwatch.parse_endpoint("host:port")

    def test_endpoint_list_splits_and_skips_blanks(self):
        parsed = netwatch.parse_endpoints("9.9.9.9:443, 1.1.1.1:443 ,")
        self.assertEqual(parsed, [("9.9.9.9", 443), ("1.1.1.1", 443)])

    def test_blank_list_is_empty(self):
        self.assertEqual(netwatch.parse_endpoints(""), [])
        self.assertEqual(netwatch.parse_endpoints(None), [])


class TestRouteParsing(unittest.TestCase):
    """Gateway parsing is split out of the probe so it is testable off-line."""

    MACOS_ROUTE = """
   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""

    def test_route_output_gateway_is_parsed(self):
        self.assertEqual(netwatch.parse_route_output(self.MACOS_ROUTE), "192.168.1.1")

    def test_route_output_without_gateway(self):
        self.assertIsNone(netwatch.parse_route_output("route: writing to routing socket\n"))

    def test_proc_net_route_default_gateway_is_parsed(self):
        table = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
            "eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
            "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
        )
        self.assertEqual(netwatch.parse_proc_net_route(table), "192.168.1.1")

    def test_proc_net_route_header_only(self):
        self.assertIsNone(netwatch.parse_proc_net_route("Iface\tDestination\tGateway\n"))


class TestProbeResultHelpers(unittest.TestCase):
    """Result normalisation keeps hand-written and real probes interchangeable."""

    def test_dict_is_passed_through(self):
        self.assertEqual(netwatch.probe_result({"result": OPEN, "ms": "12.5"}),
                         {"result": OPEN, "ms": 12.5})

    def test_bare_number_means_open(self):
        self.assertEqual(netwatch.probe_result(8.0), {"result": OPEN, "ms": 8.0})

    def test_none_means_unreachable(self):
        self.assertEqual(netwatch.probe_result(None), {"result": UNREACHABLE, "ms": None})

    def test_missing_result_defaults_to_unreachable(self):
        self.assertEqual(netwatch.probe_result({"ms": 5.0}), {"result": UNREACHABLE, "ms": 5.0})

    def test_refusal_counts_as_reachable(self):
        self.assertTrue(netwatch.is_reachable({"result": REFUSED, "ms": 1.0}))
        self.assertTrue(netwatch.is_reachable({"result": OPEN, "ms": 1.0}))
        self.assertFalse(netwatch.is_reachable({"result": UNREACHABLE, "ms": None}))

    def test_aggregate_prefers_open_then_refused(self):
        probes = [{"result": UNREACHABLE, "ms": None}, {"result": REFUSED, "ms": 2.0}]
        self.assertEqual(netwatch.aggregate_result(probes), REFUSED)
        probes.append({"result": OPEN, "ms": 9.0})
        self.assertEqual(netwatch.aggregate_result(probes), OPEN)
        self.assertEqual(netwatch.aggregate_result([]), "none")

    def test_fastest_ms_ignores_unmeasured_endpoints(self):
        probes = [{"result": UNREACHABLE, "ms": None}, {"result": OPEN, "ms": 30.0},
                  {"result": REFUSED, "ms": 4.0}]
        self.assertEqual(netwatch.fastest_ms(probes), 4.0)
        self.assertIsNone(netwatch.fastest_ms([{"result": UNREACHABLE, "ms": None}]))


class TestLoopbackDetection(unittest.TestCase):
    """A loopback target is never evidence about the link."""

    def test_loopback_hosts(self):
        for host in ("127.0.0.1", "127.5.5.5", "localhost", "LOCALHOST", "::1"):
            self.assertTrue(netwatch.is_loopback_host(host), host)

    def test_offbox_hosts(self):
        for host in ("192.168.1.10", "9.9.9.9", "2001:4860:4860::8888", "", None):
            self.assertFalse(netwatch.is_loopback_host(host), host)



class TestClassify(unittest.TestCase):
    """State precedence is the whole contract of the tool."""

    def test_ok_when_everything_answers(self):
        state = netwatch.classify(True, "192.168.1.162", 5.0,
                                  [{"result": OPEN, "ms": 12.0}],
                                  target={"result": REFUSED, "ms": 0.2},
                                  gateway={"result": OPEN, "ms": 3.0})
        self.assertEqual(state, netwatch.STATE_OK)

    def test_no_local_address_wins(self):
        state = netwatch.classify(False, None, 5.0, [{"result": OPEN, "ms": 12.0}])
        self.assertEqual(state, netwatch.STATE_NO_ROUTE)

    def test_missing_route_wins_even_when_endpoints_answer(self):
        """The literal cycle: no route, yet a stale socket still answers."""
        state = netwatch.classify(False, "192.168.1.162", 5.0, [{"result": OPEN, "ms": 1.0}])
        self.assertEqual(state, netwatch.STATE_NO_ROUTE)

    def test_transport_exhausted_when_nothing_answers(self):
        state = netwatch.classify(True, "192.168.1.162", 5.0,
                                  [{"result": UNREACHABLE, "ms": None}],
                                  target={"result": UNREACHABLE, "ms": None})
        self.assertEqual(state, netwatch.STATE_TRANSPORT_EXHAUSTED)

    def test_a_single_answering_endpoint_keeps_the_path_alive(self):
        state = netwatch.classify(True, "192.168.1.162", 5.0,
                                  [{"result": UNREACHABLE, "ms": None},
                                   {"result": OPEN, "ms": 12.0}])
        self.assertEqual(state, netwatch.STATE_OK)

    def test_refusal_alone_is_not_an_outage(self):
        """A reset proves the path works: the port is simply closed."""
        state = netwatch.classify(True, "192.168.1.162", 5.0, [{"result": REFUSED, "ms": 40.0}])
        self.assertEqual(state, netwatch.STATE_OK)

    def test_authoritative_target_alone_keeps_the_path_alive(self):
        state = netwatch.classify(True, "192.168.1.162", 5.0,
                                  [{"result": UNREACHABLE, "ms": None}],
                                  target={"result": REFUSED, "ms": 0.5})
        self.assertEqual(state, netwatch.STATE_OK)

    def test_dns_failure_when_dns_fails_but_path_answers(self):
        state = netwatch.classify(True, "192.168.1.162", None, [{"result": OPEN, "ms": 12.0}])
        self.assertEqual(state, netwatch.STATE_DNS_FAILURE)

    def test_transport_exhausted_outranks_dns_failure(self):
        state = netwatch.classify(True, "192.168.1.162", None,
                                  [{"result": UNREACHABLE, "ms": None}],
                                  target={"result": UNREACHABLE, "ms": None})
        self.assertEqual(state, netwatch.STATE_TRANSPORT_EXHAUSTED)

    def test_degraded_above_the_slow_threshold(self):
        state = netwatch.classify(True, "192.168.1.162", 5.0,
                                  [{"result": OPEN, "ms": 1200.0}], slow_ms=1000.0)
        self.assertEqual(state, netwatch.STATE_DEGRADED)

    def test_degraded_exactly_at_the_threshold(self):
        state = netwatch.classify(True, "192.168.1.162", 5.0,
                                  [{"result": OPEN, "ms": 1000.0}], slow_ms=1000.0)
        self.assertEqual(state, netwatch.STATE_DEGRADED)

    def test_slow_dns_alone_degrades(self):
        state = netwatch.classify(True, "192.168.1.162", 1500.0,
                                  [{"result": OPEN, "ms": 12.0}], slow_ms=1000.0)
        self.assertEqual(state, netwatch.STATE_DEGRADED)

    def test_gateway_failure_alone_does_not_define_state(self):
        """Gateways routinely filter TCP, so a dead gateway probe is informational."""
        state = netwatch.classify(True, "192.168.1.162", 5.0,
                                  [{"result": OPEN, "ms": 12.0}],
                                  gateway={"result": UNREACHABLE, "ms": None})
        self.assertEqual(state, netwatch.STATE_OK)

    def test_gateway_latency_counts_towards_degraded(self):
        state = netwatch.classify(True, "192.168.1.162", 5.0,
                                  [{"result": OPEN, "ms": 12.0}],
                                  gateway={"result": OPEN, "ms": 2200.0}, slow_ms=1000.0)
        self.assertEqual(state, netwatch.STATE_DEGRADED)


class TestSampling(unittest.TestCase):
    """``sample()`` is the only place probes and payload keys come together."""

    PAYLOAD_KEYS = (
        "local_addr", "gateway", "route_present", "dns_name", "dns_ms",
        "target", "target_authoritative", "internet", "gateway_probe",
        "target_result", "target_ms", "internet_result", "internet_ms",
        "state", "local_time",
    )

    def sample(self, probe, **overrides):
        kwargs = {"dns_name": "swscan.apple.com", "target": TARGET,
                  "internet": "9.9.9.9:443,1.1.1.1:443", "gateway_port": GATEWAY_PORT}
        kwargs.update(overrides)
        return netwatch.sample(probe, **kwargs)

    def test_payload_shape_and_values(self):
        payload = self.sample(FakeProbe().load(frame()))
        for key in self.PAYLOAD_KEYS:
            self.assertIn(key, payload, key)
        self.assertEqual(payload["state"], netwatch.STATE_OK)
        self.assertEqual(payload["route_present"], True)
        self.assertEqual(payload["target_result"], REFUSED)
        self.assertEqual(payload["target_ms"], 0.2)
        self.assertEqual(payload["internet_result"], OPEN)
        self.assertEqual(payload["internet_ms"], 12.0)
        self.assertEqual([item["endpoint"] for item in payload["internet"]],
                         ["9.9.9.9:443", "1.1.1.1:443"])
        self.assertEqual(payload["gateway_probe"]["endpoint"],
                         "%s:%d" % (GATEWAY, GATEWAY_PORT))

    def test_local_time_carries_a_utc_offset(self):
        payload = self.sample(FakeProbe().load(frame()))
        stamp = datetime.fromisoformat(payload["local_time"])
        self.assertIsNotNone(stamp.tzinfo)

    def test_loopback_target_is_not_authoritative(self):
        payload = self.sample(FakeProbe().load(frame()))
        self.assertFalse(payload["target_authoritative"])

    def test_offbox_target_is_authoritative(self):
        payload = self.sample(FakeProbe().load(frame()), target=LAN_TARGET)
        self.assertTrue(payload["target_authoritative"])

    def test_dns_probe_can_be_disabled(self):
        probe = FakeProbe().load(frame())
        payload = self.sample(probe, dns_name=None)
        self.assertIsNone(payload["dns_ms"])
        self.assertEqual(probe.resolved, [])

    def test_gateway_probe_is_skipped_without_a_port(self):
        payload = self.sample(FakeProbe().load(frame()), gateway_port=None)
        self.assertIsNone(payload["gateway_probe"])

    def test_missing_local_address_is_reported_as_no_route(self):
        payload = self.sample(FakeProbe().load(frame(local_addr=None)))
        self.assertEqual(payload["state"], netwatch.STATE_NO_ROUTE)

    def test_unknown_endpoint_is_unreachable_by_default(self):
        payload = self.sample(FakeProbe().load(frame()), internet="192.0.2.1:443")
        self.assertEqual(payload["internet_result"], UNREACHABLE)
        self.assertIsNone(payload["internet_ms"])


DEAD_CONNECT = {
    "9.9.9.9:443": {"result": UNREACHABLE, "ms": None},
    "1.1.1.1:443": {"result": UNREACHABLE, "ms": None},
}


class TestNetWatcher(unittest.TestCase):
    """Cycle detection, outage bookkeeping and the audit-shaped records."""

    def test_tick_record_schema_and_seq(self):
        watcher = make_watcher(FakeProbe().load(frame()))
        records = watcher.tick()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(sorted(record.keys()), ["payload", "seq", "timestamp", "type"])
        self.assertEqual(record["type"], "netwatch_tick")
        self.assertEqual(record["seq"], 1)
        self.assertTrue(record["timestamp"].endswith("+00:00"), record["timestamp"])
        self.assertEqual(record["payload"]["state"], netwatch.STATE_OK)
        self.assertEqual(watcher.cycles, 0)

    def test_seq_increments_across_ticks(self):
        watcher = make_watcher(FakeProbe().load(frame()))
        self.assertEqual(watcher.tick()[0]["seq"], 1)
        self.assertEqual(watcher.tick()[0]["seq"], 2)

    def test_healthy_ticks_emit_only_tick_records(self):
        watcher = make_watcher(FakeProbe(frames=[frame(), frame()]))
        types = [record["type"] for record in watcher.tick() + watcher.tick()]
        self.assertEqual(types, ["netwatch_tick", "netwatch_tick"])

    def test_transport_exhausted_emits_cycle_and_fallback(self):
        watcher = make_watcher(FakeProbe().load(frame(connect=DEAD_CONNECT)))
        records = watcher.tick()
        self.assertEqual([record["type"] for record in records],
                         ["netwatch_tick", "network_cycle", "network_transport_exhausted"])
        cycle = records_of(records, "network_cycle")[0]["payload"]
        self.assertEqual(cycle["cycle"], 1)
        self.assertEqual(cycle["previous_state"], netwatch.STATE_OK)
        self.assertEqual(cycle["internet_result"], UNREACHABLE)
        fallback = records_of(records, "network_transport_exhausted")[0]["payload"]
        self.assertEqual(fallback["severity"], "pause")
        self.assertEqual(fallback["target"]["endpoint"], TARGET)
        self.assertEqual([item["endpoint"] for item in fallback["internet"]],
                         ["9.9.9.9:443", "1.1.1.1:443"])
        self.assertEqual(watcher.cycles, 1)

    def test_first_bad_tick_is_already_a_cycle(self):
        watcher = make_watcher(FakeProbe().load(frame(connect=DEAD_CONNECT)))
        watcher.tick()
        self.assertEqual(watcher.cycles, 1)

    def test_sustained_outage_does_not_recount_cycles(self):
        probe = FakeProbe(frames=[frame(connect=DEAD_CONNECT) for _ in range(3)])
        watcher = make_watcher(probe)
        watcher.tick()
        watcher.tick()
        self.assertEqual([record["type"] for record in watcher.tick()], ["netwatch_tick"])
        self.assertEqual(watcher.cycles, 1)

    def test_recovery_emits_duration_and_closes_the_outage(self):
        probe = FakeProbe(frames=[frame(connect=DEAD_CONNECT), frame(connect=DEAD_CONNECT), frame()])
        watcher = make_watcher(probe)
        watcher.tick()
        watcher.tick()
        records = watcher.tick()
        recovered = records_of(records, "network_recovered")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["payload"]["previous_state"],
                         netwatch.STATE_TRANSPORT_EXHAUSTED)
        self.assertGreater(recovered[0]["payload"]["outage_seconds"], 0.0)
        self.assertEqual(len(watcher.outages), 1)
        self.assertEqual(watcher.outages[0]["state"], netwatch.STATE_TRANSPORT_EXHAUSTED)
        self.assertIsNotNone(watcher.outages[0]["end"])
        self.assertEqual(watcher.state, netwatch.STATE_OK)
        self.assertEqual(watcher.cycles, 1)

    def test_dns_failure_is_a_cycle_without_the_fallback_record(self):
        watcher = make_watcher(FakeProbe().load(frame(dns_ms=None)))
        records = watcher.tick()
        self.assertEqual([record["type"] for record in records],
                         ["netwatch_tick", "network_cycle"])
        self.assertEqual(records[1]["payload"]["state"], netwatch.STATE_DNS_FAILURE)
        self.assertEqual(watcher.cycles, 1)

    def test_no_route_is_a_cycle(self):
        watcher = make_watcher(FakeProbe().load(frame(local_addr=None)))
        records = watcher.tick()
        self.assertEqual([record["type"] for record in records],
                         ["netwatch_tick", "network_cycle"])
        self.assertEqual(records[1]["payload"]["state"], netwatch.STATE_NO_ROUTE)

    def test_degraded_is_never_a_cycle(self):
        watcher = make_watcher(FakeProbe().load(
            frame(connect={"9.9.9.9:443": {"result": OPEN, "ms": 2500.0}})))
        self.assertEqual([record["type"] for record in watcher.tick()], ["netwatch_tick"])
        self.assertEqual(watcher.state, netwatch.STATE_DEGRADED)
        self.assertEqual(watcher.cycles, 0)
        self.assertEqual(watcher.outages, [])


class TestSummaryAndFormatting(unittest.TestCase):
    """The summary is what an operator reads after an overnight soak."""

    def test_summary_counts_states_and_outages(self):
        probe = FakeProbe(frames=[frame(connect=DEAD_CONNECT), frame()])
        watcher = make_watcher(probe)
        watcher.tick()
        watcher.tick()
        summary = watcher.summary()
        self.assertEqual(summary["ticks"], 2)
        self.assertEqual(summary["cycles"], 1)
        self.assertEqual(summary["states"][netwatch.STATE_OK], 1)
        self.assertEqual(summary["states"][netwatch.STATE_TRANSPORT_EXHAUSTED], 1)
        self.assertEqual(len(summary["outages"]), 1)
        self.assertGreater(summary["longest_outage_seconds"], 0.0)
        self.assertEqual(summary["outage_seconds_total"], summary["longest_outage_seconds"])
        self.assertEqual(summary["target"], TARGET)

    def test_summary_reports_a_still_open_outage(self):
        watcher = make_watcher(FakeProbe().load(frame(connect=DEAD_CONNECT)))
        watcher.tick()
        outage = watcher.summary()["outages"][0]
        self.assertIsNone(outage["end"])
        self.assertGreaterEqual(outage["outage_seconds"], 0.0)

    def test_summary_lines_are_operator_readable(self):
        probe = FakeProbe(frames=[frame(connect=DEAD_CONNECT), frame()])
        watcher = make_watcher(probe)
        watcher.tick()
        watcher.tick()
        lines = watcher.summary_lines()
        self.assertTrue(lines[0].startswith("netwatch: 2 ticks, 1 cycles"))
        self.assertIn("ok=1", lines[1])
        self.assertIn("transport_exhausted=1", lines[1])
        self.assertTrue(lines[3].startswith("  outage #1:"), lines[3])
        self.assertIn(netwatch.STATE_TRANSPORT_EXHAUSTED, lines[3])

    def test_format_record_lines(self):
        watcher = make_watcher(FakeProbe().load(frame(connect=DEAD_CONNECT)))
        lines = [netwatch.format_record(record) for record in watcher.tick()]
        self.assertIn("transport_exhausted", lines[0])
        self.assertIn("target=refused/0.2ms", lines[0])
        self.assertIn("internet=unreachable", lines[0])
        self.assertIn("CYCLE #1", lines[1])
        self.assertIn("FALLBACK network_transport_exhausted severity=pause", lines[2])

    def test_format_helpers_handle_missing_values(self):
        self.assertEqual(netwatch._format_ms(None), "-")
        self.assertEqual(netwatch._format_ms(12.34), "12.3ms")
        self.assertEqual(netwatch._format_seconds(30.0), "30.0s")
        self.assertEqual(netwatch._format_seconds(90.0), "1m30s")
        self.assertEqual(netwatch._format_seconds("nonsense"), "?")


class TestFallbackVocabulary(unittest.TestCase):
    """netwatch must speak the same trigger names and severities as the bridge."""

    def test_severity_matches_the_shugonet_fallback_table(self):
        self.assertEqual(netwatch.NETWORK_TRANSPORT_SEVERITY, "pause")
        if NETWORK_FALLBACK_SEVERITIES:
            self.assertEqual(netwatch.NETWORK_TRANSPORT_SEVERITY,
                             NETWORK_FALLBACK_SEVERITIES["network_transport_exhausted"])

    def test_emitted_record_type_is_the_bridge_trigger_name(self):
        watcher = make_watcher(FakeProbe().load(frame(connect=DEAD_CONNECT)))
        emitted = {record["type"] for record in watcher.tick()}
        self.assertIn("network_transport_exhausted", emitted)
        if NETWORK_FALLBACK_SEVERITIES:
            self.assertIn("network_transport_exhausted", NETWORK_FALLBACK_SEVERITIES)


class TestRunLoop(unittest.TestCase):
    """``run()`` owns the sampling cadence; the probe owns time."""

    def test_run_samples_the_requested_number_of_ticks(self):
        probe = FakeProbe(frames=[frame(), frame(), frame()])
        watcher = make_watcher(probe)
        summary = watcher.run(interval=2.0, ticks=3)
        self.assertEqual(summary["ticks"], 3)
        self.assertEqual(probe.sleeps, [2.0, 2.0])  # between ticks, not after the last

    def test_run_stops_at_the_duration_bound(self):
        probe = FakeProbe(frames=[frame() for _ in range(20)], step=0.1)
        watcher = make_watcher(probe)
        summary = watcher.run(interval=1.0, duration=2.5)
        self.assertLess(summary["ticks"], 20)
        self.assertGreaterEqual(probe.clock, 2.5)

    def test_run_reports_every_record_through_the_callback(self):
        probe = FakeProbe(frames=[frame(connect=DEAD_CONNECT), frame()])
        watcher = make_watcher(probe)
        seen = []
        watcher.run(interval=0.5, ticks=2, on_record=seen.append)
        self.assertEqual([record["type"] for record in seen],
                         ["netwatch_tick", "network_cycle", "network_transport_exhausted",
                          "netwatch_tick", "network_recovered"])
        self.assertEqual(watcher.cycles, 1)

    def test_duration_zero_samples_once(self):
        probe = FakeProbe(frames=[frame(), frame()])
        watcher = make_watcher(probe)
        summary = watcher.run(interval=1.0, duration=0.0)
        self.assertEqual(summary["ticks"], 1)


class TestJsonlWriter(unittest.TestCase):
    """The sink is plain JSONL: one record per line, audit field names."""

    def test_write_creates_one_line_per_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "netwatch.jsonl"
            watcher = make_watcher(FakeProbe().load(frame()))
            writer = netwatch.JsonlWriter(path)
            for record in watcher.tick():
                writer.write(record)
            writer.close()
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(sorted(json.loads(lines[0]).keys()),
                             ["payload", "seq", "timestamp", "type"])

    def test_writer_appends_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "netwatch.jsonl"
            with netwatch.JsonlWriter(path) as writer:
                writer.write({"type": "first"})
            with netwatch.JsonlWriter(path) as writer:
                writer.write({"type": "second"})
            types = [json.loads(line)["type"] for line in path.read_text().splitlines()]
            self.assertEqual(types, ["first", "second"])

    def test_write_opens_on_demand_and_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "netwatch.jsonl"
            writer = netwatch.JsonlWriter(path)
            writer.close()  # never opened
            writer.write({"type": "late"})
            writer.close()
            writer.close()
            self.assertTrue(path.exists())


class TestMainCli(unittest.TestCase):
    """The CLI contract: exit 0 healthy, 1 when a cycle was seen, 2 on misuse."""

    def run_cli(self, argv, frames):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "netwatch.jsonl"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = netwatch.main(["--jsonl", str(path)] + argv,
                                     probe_factory=make_factory(frames))
            records = []
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                records = [json.loads(line) for line in text.splitlines() if line]
            return code, records, stdout.getvalue()

    def test_healthy_run_exits_zero_and_ends_with_a_summary(self):
        code, records, output = self.run_cli(["--ticks", "2", "--interval", "0"],
                                             [frame(), frame()])
        self.assertEqual(code, 0)
        self.assertEqual([record["type"] for record in records],
                         ["netwatch_tick", "netwatch_tick", "netwatch_summary"])
        self.assertEqual(records[-1]["payload"]["cycles"], 0)
        self.assertEqual(records[-1]["payload"]["ticks"], 2)
        self.assertIn("netwatch: 2 ticks, 0 cycles", output)

    def test_cycle_run_exits_one(self):
        code, records, output = self.run_cli(["--ticks", "1"], [frame(connect=DEAD_CONNECT)])
        self.assertEqual(code, 1)
        self.assertEqual(records[-1]["type"], "netwatch_summary")
        self.assertEqual(records[-1]["payload"]["cycles"], 1)
        self.assertIn("network_transport_exhausted", output)

    def test_quiet_mode_suppresses_tick_lines(self):
        code, records, output = self.run_cli(["--ticks", "1", "--quiet"], [frame()])
        self.assertEqual(code, 0)
        self.assertTrue(records)
        self.assertNotIn("dns=", output)

    def test_invalid_target_exits_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(netwatch.main(["--target", ":", "--ticks", "1"]), 2)

    def test_empty_internet_list_exits_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(netwatch.main(["--internet", "", "--ticks", "1"]), 2)

    def test_probe_factory_receives_the_timeout(self):
        captured = {}

        def factory(timeout=1.5):
            captured["timeout"] = timeout
            return FakeProbe(frames=[frame()])

        with contextlib.redirect_stdout(io.StringIO()):
            code = netwatch.main(["--ticks", "1", "--timeout", "3.5"], probe_factory=factory)
        self.assertEqual(code, 0)
        self.assertEqual(captured["timeout"], 3.5)

    def test_parser_defaults_match_the_module_constants(self):
        args = netwatch.build_parser().parse_args([])
        self.assertEqual(args.target, netwatch.DEFAULT_TARGET)
        self.assertEqual(args.internet, netwatch.DEFAULT_INTERNET)
        self.assertEqual(args.slow_ms, netwatch.DEFAULT_SLOW_MS)
        self.assertIsNone(args.duration)
        self.assertIsNone(args.ticks)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()






