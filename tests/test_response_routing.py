"""The hive answers through the device closest to the operator.

Two layers are checked: the proximity policy itself (pure scoring/selection) and
the live behaviour -- a response on a device with no speaker is delegated to the
node that has one, only the primary may direct another node, and an inbound
`send` frame actually reaches the agent instead of being silently acked.
"""
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import response_routing as rr  # noqa: E402
import shugocore_agent as sa  # noqa: E402
from agent_runtime import ShugonetAgentRuntime  # noqa: E402


class ProximityPolicyTestCase(unittest.TestCase):
    def test_directed_speech_and_face_outweighs_a_device_with_nothing(self):
        near, why = rr.proximity_score({
            "remote_face_present": True, "remote_gaze_toward_camera": True,
            "remote_speech_source": "instruction_directed"})
        far, _ = rr.proximity_score({})
        self.assertGreater(near, far)
        self.assertIn("face", why)
        self.assertEqual(far, 0.0)

    def test_ambient_audio_is_not_an_operator(self):
        score, why = rr.proximity_score({"voice_active": True,
                                         "speech_source": "ambient_noise"})
        self.assertLess(score, rr.DEFAULT_FLOOR)
        self.assertNotIn("speech:ambient_noise", why)

    def test_absent_attention_vetoes_a_quiet_device(self):
        score, why = rr.proximity_score({"attention_state": "absent"})
        self.assertEqual(score, 0.0)
        self.assertIn("attention:absent", why)

    def test_stale_evidence_decays_then_gives_up(self):
        facts = {"face_present": True}
        fresh, _ = rr.proximity_score(facts)
        aged, why = rr.proximity_score(facts, age_s=20)
        self.assertLess(aged, fresh)
        self.assertIn("aged(20s)", why)
        stale, why = rr.proximity_score(facts, age_s=120)
        self.assertEqual(stale, 0.0)
        self.assertIn("stale(120s)", why)

    def test_selection_prefers_the_closest_speaker(self):
        chosen, why = rr.select([
            {"device_id": "desktop", "facts": {}, "can_speak": False},
            {"device_id": "tab", "facts": {"face_present": True,
                                           "speech_source": "instruction_directed"}},
            {"device_id": "a51", "facts": {"face_present": True}},
        ])
        self.assertEqual(chosen["device_id"], "tab")
        self.assertIn("closest to the operator", why)

    def test_the_only_speaker_wins_even_from_far_away(self):
        chosen, _ = rr.select([
            {"device_id": "desktop", "facts": {"face_present": True},
             "can_speak": False},
            {"device_id": "tab", "facts": {"face_present": True}},
        ])
        self.assertEqual(chosen["device_id"], "tab")

    def test_nobody_present_means_no_answer_target(self):
        chosen, why = rr.select([{"device_id": "tab", "facts": {}}])
        self.assertIsNone(chosen)
        self.assertIn("nobody reports the operator present", why)

    def test_no_speaker_anywhere_means_no_target(self):
        chosen, why = rr.select([{"device_id": "desktop", "facts": {},
                                  "can_speak": False}])
        self.assertIsNone(chosen)
        self.assertIn("no device reports speech output", why)

    def test_operator_can_force_a_device(self):
        chosen, why = rr.select(
            [{"device_id": "tab", "facts": {"face_present": True}},
             {"device_id": "a51", "facts": {}}], forced="a51")
        self.assertEqual(chosen["device_id"], "a51")
        self.assertIn("forced", why)
        chosen, why = rr.select([{"device_id": "desktop", "facts": {},
                                  "can_speak": False}], forced="desktop")
        self.assertIsNone(chosen)
        self.assertIn("no speech output", why)

    def test_ties_are_broken_deterministically(self):
        rows = [{"device_id": "b", "facts": {"face_present": True}},
                {"device_id": "a", "facts": {"face_present": True}}]
        self.assertEqual([c["device_id"] for c in rr.rank(rows)], ["a", "b"])


class FakeElection:
    def __init__(self, node_id="shugo-desktop", primary="shugo-desktop",
                 priority=10):
        self.node_id = node_id
        self.priority = priority
        self._primary = primary

    def tick(self):
        return {"primary": self._primary, "role": "primary"}

    def primary(self):
        return self._primary

    def live_peers(self):
        return []


class _Speaker:
    """Stand-in for the Kotlin TTS bridge."""

    def __init__(self):
        self.said = []

    def speak(self, text):
        self.said.append(text)
        return True


def _probe_agent(node_id="shugo-desktop", primary="shugo-desktop"):
    agent = sa.AndroidAgent.__new__(sa.AndroidAgent)
    agent.device_caps = node_id
    agent.telemetry = {}
    agent.policy = {}
    agent.mesh_election = FakeElection(node_id=node_id, primary=primary)
    agent.shugonet_runtime = None
    agent._speak_listener = None
    agent._peer_tts = {}
    agent._delegated_results = []
    agent._delegated_out = 0
    agent._delegated_from = None
    agent._last_route = None
    agent.last_observation = {}
    agent._mesh_mem_headroom = lambda: 1 << 30
    agent._get_battery = lambda: 90
    agent.log = lambda *a, **k: None
    return agent


class AgentRoutingTestCase(unittest.TestCase):
    def test_candidates_include_self_and_each_peer(self):
        agent = _probe_agent()
        agent.last_observation = {"mesh_peers": [
            {"device_id": "tab", "remote_face_present": True,
             "remote_speech_source": "instruction_directed"},
            {"device_id": "a51", "remote_face_present": False},
        ]}
        devices = {c["device_id"]: c for c in agent._response_candidates()}
        self.assertIn("shugo-desktop", devices)
        self.assertFalse(devices["shugo-desktop"]["can_speak"])   # no TTS here
        self.assertTrue(devices["tab"]["can_speak"])              # phones do

    def test_the_primary_delegates_to_the_phone_with_the_operator(self):
        agent = _probe_agent()
        agent.last_observation = {"mesh_peers": [
            {"device_id": "tab", "remote_face_present": True,
             "remote_gaze_toward_camera": True,
             "remote_speech_source": "instruction_directed"}]}
        sent = []

        class _Runtime:
            def send(self, peer, topic, payload):
                sent.append((peer, topic, payload))
                return {"status": "success"}

        agent.shugonet_runtime = _Runtime()
        chosen, _ = agent.select_response_node()
        self.assertEqual(chosen["device_id"], "tab")
        result = agent._route_response("speak", {"text": "the kettle is boiled"})
        self.assertEqual(result["status"], "delegated")
        self.assertEqual(result["delegated_to"], "tab")
        self.assertEqual(sent[0][0], "tab")
        self.assertEqual(sent[0][1], sa.DELEGATE_TOPIC)
        self.assertEqual(sent[0][2]["params"]["text"], "the kettle is boiled")
        self.assertEqual(agent._last_route["device"], "tab")

    def test_a_device_with_a_speaker_answers_locally(self):
        agent = _probe_agent()
        agent._speak_listener = _Speaker()
        agent.last_observation = {}          # nobody else is present
        self.assertIsNone(agent._route_response("speak", {"text": "hi"}))

    def test_nobody_present_is_recorded_not_guessed(self):
        # A hub with no speaker of its own says exactly that ...
        agent = _probe_agent()
        self.assertIsNone(agent._route_response("speak", {"text": "hi"}))
        self.assertIn("no device reports speech output",
                      agent._last_route["reason"])
        # ... and a device that could speak, with nobody near it, says this.
        speaking = _probe_agent()
        speaking._speak_listener = _Speaker()
        speaking.last_observation = {"mesh_peers": [{"device_id": "tab"}]}
        speaking._peer_tts["tab"] = True
        self.assertIsNone(speaking._route_response("speak", {"text": "hi"}))
        self.assertIn("nobody reports the operator present",
                      speaking._last_route["reason"])


class DelegationAuthorityTestCase(unittest.TestCase):
    def test_only_the_primary_may_direct_a_node(self):
        follower = _probe_agent(node_id="android-tab", primary="shugo-desktop")
        self.assertFalse(follower._mesh_may_act("speak"))
        self.assertTrue(follower._mesh_may_act("speak",
                                              delegated_by="shugo-desktop"))
        self.assertFalse(follower._mesh_may_act("speak",
                                               delegated_by="shugo-a51"))

    def test_a_delegated_speak_runs_on_the_named_device(self):
        tab = _probe_agent(node_id="android-tab", primary="shugo-desktop")
        speaker = _Speaker()
        tab._speak_listener = speaker
        tab.interaction = None
        tab.conversation = None
        replies = []
        tab._mesh_reply = lambda peer, payload: replies.append((peer, payload))
        tab._handle_delegated_action("shugo-desktop", {
            "action_type": "speak", "params": {"text": "Answering from the Tab"},
            "id": "q1"})
        self.assertEqual(speaker.said, ["Answering from the Tab"])
        self.assertEqual(replies[0][0], "shugo-desktop")
        self.assertEqual(replies[0][1]["status"], "success")
        self.assertTrue(replies[0][1]["delivered"])

    def test_a_non_primary_sender_is_refused(self):
        tab = _probe_agent(node_id="android-tab", primary="shugo-desktop")
        tab._speak_listener = _Speaker()
        replies = []
        tab._mesh_reply = lambda peer, payload: replies.append(payload)
        tab._handle_delegated_action("shugo-a51", {
            "action_type": "speak", "params": {"text": "nope"}, "id": "q2"})
        self.assertEqual(replies[0]["status"], "refused")
        self.assertIn("not the primary", replies[0]["reason"])

    def test_non_delegatable_actions_are_refused(self):
        tab = _probe_agent(node_id="android-tab", primary="shugo-desktop")
        replies = []
        tab._mesh_reply = lambda peer, payload: replies.append(payload)
        tab._handle_delegated_action("shugo-desktop", {
            "action_type": "api_call", "params": {"url": "https://x"}, "id": "q3"})
        self.assertIn("not delegatable", replies[0]["reason"])


class InboundSendTransportTestCase(unittest.TestCase):
    def test_an_inbound_send_reaches_the_handler(self):
        """Before this, `send` frames were acked by the transport and dropped."""
        import time

        def _free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])

        received = []
        listener = ShugonetAgentRuntime(agent_id="android-tab",
                                        host="127.0.0.1", port=_free_port(),
                                        heartbeat_interval=0)
        listener.set_send_handler(
            lambda peer, topic, payload: received.append((peer, topic, payload)))
        listener.start()
        sender = ShugonetAgentRuntime(agent_id="shugo-desktop",
                                      host="127.0.0.1", port=_free_port(),
                                      heartbeat_interval=0)
        sender.add_peer("android-tab", "127.0.0.1", listener.status()["port"])
        sender.start()
        try:
            self.assertTrue(sender.reconnect_peers() >= 1)
            result = sender.send("android-tab", sa.DELEGATE_TOPIC,
                                 {"action_type": "speak",
                                  "params": {"text": "hello from the hub"}})
            self.assertEqual(result["status"], "success")
            for _ in range(100):
                if received:
                    break
                time.sleep(0.05)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][0], "shugo-desktop")
            self.assertEqual(received[0][1], sa.DELEGATE_TOPIC)
            self.assertEqual(received[0][2]["params"]["text"],
                             "hello from the hub")
        finally:
            sender.stop()
            listener.stop()


if __name__ == "__main__":
    unittest.main()


