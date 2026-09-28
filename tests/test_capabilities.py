"""The hive's capability map: who offers what, and why nobody does.

Two properties matter, and both were learned on the layer split where a peer's claim and a
working socket turned out to be different things: advertised is not usable, and "nobody"
needs a reason -- a silent peer and a fleet that never claimed the capability otherwise
look identical to an operator.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capabilities as caps  # noqa: E402

PEERS = [
    {"node_id": "shugo-mac", "role": "primary-capable",
     "mem_available_bytes": 1_900_000_000,
     "caps": {"persona": "http://192.168.1.162:11434", "capacity": ""}},
    {"node_id": "shugo-tab", "role": "follower", "mem_available_bytes": 160_000_000,
     "caps": {"perception": "", "capacity": ""}},
]


class CapabilityMapTestCase(unittest.TestCase):
    def test_an_advertisement_is_bounded_and_only_known_names(self):
        self.assertEqual(caps.normalise({"persona": "x", "bogus": "y", "capacity": ""}),
                         {"persona": "x", "capacity": ""})
        self.assertEqual(caps.normalise(["perception", "perception"]),
                         {"perception": ""})
        self.assertEqual(caps.normalise(None), {})
        self.assertEqual(len(caps.normalise({"persona": "x" * 500})["persona"]),
                         caps.MAX_LOCATOR_CHARS)

    def test_a_node_advertises_only_what_it_runs(self):
        self.assertEqual(caps.claim(reasoning=True, capacity=True),
                         {"reasoning": "", "capacity": ""})
        self.assertEqual(caps.claim(persona="http://m:1"), {"persona": "http://m:1"})

    def test_the_map_resolves_a_service_to_the_node_offering_it(self):
        resolved = caps.CapabilityMap(PEERS, verify=False).resolve("persona")
        self.assertEqual(resolved["node"], "shugo-mac")
        self.assertEqual(resolved["locator"], "http://192.168.1.162:11434")

    def test_nobody_offering_it_says_so_with_a_reason(self):
        resolved = caps.CapabilityMap(PEERS, verify=False).resolve("reasoning")
        self.assertIn("no live peer advertises", resolved["reason"])
        self.assertIn("2 live", resolved["reason"])

    def test_an_empty_fleet_is_a_different_reason_from_a_silent_fleet(self):
        self.assertIn("no live peers",
                      caps.CapabilityMap([], verify=False).resolve("persona")["reason"])

    def test_an_unknown_capability_is_refused_not_guessed(self):
        self.assertIn("unknown capability",
                      caps.CapabilityMap(PEERS, verify=False).resolve("wizardry")["reason"])

    def test_an_advertised_locator_that_does_not_answer_is_not_offered(self):
        """A stale advertisement must not stall the hive."""
        def _refuse(*_args, **_kwargs):
            raise OSError("connection refused")

        resolved = caps.CapabilityMap(PEERS, verify=True,
                                      connect=_refuse).resolve("persona")
        self.assertNotIn("node", resolved)
        self.assertIn("unreachable", resolved["reason"])
        self.assertFalse(resolved["offered"][0]["reachable"])

    def test_the_better_node_wins_when_two_offer_the_same_service(self):
        peers = [dict(PEERS[1], caps={"capacity": ""}),
                 dict(PEERS[0], caps={"capacity": ""})]
        self.assertEqual(
            caps.CapabilityMap(peers, verify=False).resolve("capacity")["node"],
            "shugo-mac")                                    # most measured headroom

    def test_the_summary_names_a_node_or_none_for_each_capability(self):
        summary = caps.CapabilityMap(PEERS, verify=False).summary()
        self.assertIn("persona->shugo-mac", summary)
        self.assertIn("reasoning->none", summary)

    def test_address_of_strips_the_scheme_and_path(self):
        self.assertEqual(
            caps.address_of("http://mac:11434/v1/chat/completions"), "mac:11434")
        self.assertEqual(caps.address_of("mac:11434"), "mac:11434")
        self.assertEqual(caps.address_of(""), "")


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class AdvertisedLocatorTestCase(unittest.TestCase):
    """A locator must be usable by a *peer*, or not advertised at all."""

    def test_a_loopback_locator_is_rewritten_to_the_reachable_address(self):
        self.assertEqual(
            caps.advertised_locator("http://127.0.0.1:11434", host="10.0.0.5"),
            "http://10.0.0.5:11434")
        self.assertEqual(
            caps.advertised_locator("127.0.0.1:11434/v1", host="10.0.0.5"),
            "10.0.0.5:11434/v1")

    def test_an_already_reachable_locator_is_left_exactly_as_configured(self):
        self.assertEqual(caps.advertised_locator("http://192.168.1.162:11434"),
                         "http://192.168.1.162:11434")
        self.assertEqual(caps.advertised_locator(""), "")

    def test_a_loopback_claim_with_no_learnable_address_is_dropped(self):
        """Better no claim than an unusable one."""
        with mock.patch.object(caps, "local_address", return_value=""):
            self.assertEqual(caps.advertised_locator("http://127.0.0.1:11434"), "")

    def test_serving_a_backend_is_told_apart_from_calling_one(self):
        self.assertTrue(caps.is_local_locator("http://127.0.0.1:11434"))
        self.assertTrue(caps.is_local_locator("localhost:11434"))
        self.assertFalse(caps.is_local_locator("http://192.168.1.162:11434"))

    def test_a_peers_loopback_claim_is_read_at_the_address_it_is_dialled(self):
        seen = []

        def _connect(address, **_kwargs):
            seen.append(address)
            return _Conn()

        resolved = caps.CapabilityMap(
            [{"node_id": "shugo-mac", "mem_available_bytes": 10 ** 9,
              "caps": {"persona": "http://127.0.0.1:11434"}}],
            verify=True, connect=_connect,
            hosts={"shugo-mac": "192.168.1.162:9000"}).resolve("persona")
        self.assertEqual(resolved["locator"], "http://192.168.1.162:11434")
        self.assertEqual(seen, [("192.168.1.162", 11434)])
        self.assertIn("at the peer's own address", resolved["reason"])


if __name__ == "__main__":
    unittest.main()
