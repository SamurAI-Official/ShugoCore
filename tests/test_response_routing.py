"""The hive answers through the device closest to the operator.

Two layers are checked: the proximity policy itself (pure scoring/selection) and
the live behaviour -- a response on a device with no speaker is delegated to the
node that has one, only the primary may direct another node, and an inbound
`send` frame actually reaches the agent instead of being silently acked.
"""
import os
import socket
import sys
import time
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

    def test_presence_alone_cannot_cross_the_floor(self):
        alone, why = rr.proximity_score({"presence_present": True})
        self.assertLess(alone, rr.DEFAULT_FLOOR)
        self.assertIn("present", why)
        # A present human who is also making noise is worth answering.
        together, _ = rr.proximity_score({"presence_present": True,
                                          "voice_active": True})
        self.assertGreaterEqual(together, rr.DEFAULT_FLOOR)

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

    def test_a_failed_delegated_speak_reports_why(self):
        """A delegated speak that fails must say what went wrong.

        The sender has no other way to learn it: the outcome travels as data, and
        "error" with no reason is exactly the silence this path exists to remove.
        """
        class _Broken:
            def speak(self, text):
                raise RuntimeError("tts engine not ready")

        tab = _probe_agent(node_id="android-tab", primary="shugo-desktop")
        tab._speak_listener = _Broken()
        tab.interaction = None
        tab.conversation = None
        replies = []
        tab._mesh_reply = lambda peer, payload: replies.append(payload)
        tab._handle_delegated_action("shugo-desktop", {
            "action_type": "speak", "params": {"text": "hello"}, "id": "q3"})
        self.assertEqual(replies[0]["status"], "error")
        self.assertIn("speak_listener_failed: RuntimeError", replies[0]["reason"])
        self.assertIn("tts engine not ready", replies[0]["reason"])

    def test_a_platform_refused_speak_is_not_reported_as_success(self):
        """The platform held the text and refused it -- that is not a success."""
        class _Refusing:
            def speak(self, text):
                return False

        tab = _probe_agent(node_id="android-tab", primary="shugo-desktop")
        tab._speak_listener = _Refusing()
        tab.interaction = None
        tab.conversation = None
        replies = []
        tab._mesh_reply = lambda peer, payload: replies.append(payload)
        tab._handle_delegated_action("shugo-desktop", {
            "action_type": "speak", "params": {"text": "hello"}, "id": "q4"})
        self.assertEqual(replies[0]["status"], "error")
        self.assertFalse(replies[0]["delivered"])
        self.assertEqual(replies[0]["reason"], "speak_listener_returned_false")

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


class AdvertisementCarriesPresenceTestCase(unittest.TestCase):
    """A device's advertisement must say where it is and whether it can speak.

    The primary measures proximity from what each node advertises. A device that
    sees the operator but never says so is invisible to the router, and the hive
    answers a person standing in front of a phone with silence.
    """

    def _advertising_node(self, node_id, human, can_speak=True):
        from mesh_election import MeshElection
        agent = _probe_agent(node_id=node_id, primary="shugo-desktop")
        agent.node_id = node_id
        agent.mesh_election = MeshElection(node_id, priority=500)
        agent.last_observation = {"human": human}
        if can_speak:
            agent._speak_listener = _Speaker()
        return agent

    def test_the_advertisement_carries_presence_and_speaker_capability(self):
        tab = self._advertising_node("shugo-tab", {
            "face_count": 1, "gaze_toward_camera": True,
            "speech_source": "instruction_directed", "speech_recent": True})
        payload = tab._mesh_heartbeat_payload()
        self.assertTrue(payload["can_speak"])
        self.assertEqual(payload["remote_face_count"], 1)
        self.assertTrue(payload["remote_face_present"])
        self.assertTrue(payload["remote_gaze_toward_camera"])
        self.assertEqual(payload["remote_speech_source"], "instruction_directed")

    def test_a_device_that_perceived_nothing_advertises_no_presence(self):
        quiet = self._advertising_node(
            "shugo-a16", {"face_count": 0, "speech_source": "none"})
        payload = quiet._mesh_heartbeat_payload()
        self.assertNotIn("remote_gaze_toward_camera", payload)
        self.assertFalse(any(key.startswith("remote_speech_source")
                             and payload[key] not in ("", "none")
                             for key in payload))

    def test_the_hub_places_a_peer_from_its_advertisement(self):
        hub = _probe_agent(node_id="shugo-desktop")
        hub.node_id = "shugo-desktop"
        tab = self._advertising_node("shugo-tab", {
            "face_count": 1, "gaze_toward_camera": True,
            "speech_source": "instruction_directed"})
        hub._mesh_heartbeat_received(tab._mesh_heartbeat_payload())
        peers = hub.telemetry["mesh_peers"]
        self.assertEqual(peers[0]["device_id"], "shugo-tab")
        self.assertTrue(peers[0]["can_speak"])
        self.assertTrue(peers[0]["remote_face_present"])
        self.assertTrue(hub._peer_tts["shugo-tab"])
        # The tick folds the merged advertisements into the observation, which is
        # what the router reads; do the same step here.
        hub.last_observation = {"mesh_peers": peers}
        chosen, why = hub.select_response_node()
        self.assertEqual(chosen["device_id"], "shugo-tab")
        self.assertIn("closest to the operator", why)

    def test_a_peer_that_reports_no_speaker_is_never_the_mouth(self):
        hub = _probe_agent(node_id="shugo-desktop")
        hub.node_id = "shugo-desktop"
        mute = self._advertising_node("shugo-mac", {
            "face_count": 1, "speech_source": "instruction_directed"},
            can_speak=False)
        hub._mesh_heartbeat_received(mute._mesh_heartbeat_payload())
        hub.last_observation = {"mesh_peers": hub.telemetry["mesh_peers"]}
        chosen, why = hub.select_response_node()
        self.assertIsNone(chosen)
        self.assertIn("no device reports speech output", why)

    def test_a_phone_without_a_camera_still_reports_it_can_hear_someone(self):
        """The A51's camera can be refused by policy; the microphone still knows.

        Voice energy and the fused scene verdict arrive in telemetry under the
        shell's own spellings, and the interaction bus reports the human's
        presence, so a camera-less phone is still a place the operator can be.
        """
        class _Bus:
            def human_context(self):
                return {"presence": "user_present",
                        "speech_source": "instruction_directed"}

        hub = _probe_agent(node_id="shugo-desktop")
        hub.node_id = "shugo-desktop"
        tab = self._advertising_node("shugo-tab", {})
        tab.interaction = _Bus()
        tab.telemetry = {"voice_active": True,
                         "scene_speech_source": "instruction_directed"}
        payload = tab._mesh_heartbeat_payload()
        self.assertTrue(payload["remote_voice_active"])
        self.assertTrue(payload["remote_presence_present"])
        self.assertEqual(payload["remote_speech_source"], "instruction_directed")
        hub._mesh_heartbeat_received(payload)
        chosen, why = hub.select_response_node()
        self.assertEqual(chosen["device_id"], "shugo-tab")
        self.assertIn("voice", why)

    def test_the_router_reads_the_merged_store_not_only_the_observation(self):
        """A host with no shell never populates the observation's peer list.

        The mesh writes ``telemetry['mesh_peers']`` the moment an advertisement
        arrives; the observation is built later (on a phone, by the Kotlin shell).
        A routed `--say` runs before the first tick, so reading only the
        observation meant no peer candidate existed and the answer was always
        "no device reports speech output".
        """
        hub = _probe_agent(node_id="shugo-desktop")
        hub.node_id = "shugo-desktop"
        tab = self._advertising_node("shugo-tab", {
            "face_count": 1, "gaze_toward_camera": True,
            "speech_source": "instruction_directed"})
        hub._mesh_heartbeat_received(tab._mesh_heartbeat_payload())
        hub.last_observation = {}                 # no tick has run yet
        chosen, why = hub.select_response_node()
        self.assertEqual(chosen["device_id"], "shugo-tab")
        self.assertIn("closest to the operator", why)

    def test_stale_advertisements_stop_placing_a_peer(self):
        hub = _probe_agent(node_id="shugo-desktop")
        hub.node_id = "shugo-desktop"
        tab = self._advertising_node("shugo-tab", {"face_count": 1})
        payload = tab._mesh_heartbeat_payload()
        payload["received_at"] = None
        hub._mesh_heartbeat_received(payload)
        peers = hub.telemetry["mesh_peers"]
        peers[0]["received_at"] = time.time() - 120      # two minutes ago
        hub.last_observation = {"mesh_peers": peers}
        chosen, why = hub.select_response_node()
        self.assertIsNone(chosen)
        self.assertIn("nobody reports the operator present", why)


if __name__ == "__main__":
    unittest.main()


