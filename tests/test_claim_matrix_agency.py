"""The agency parsers: what counts as proof that the hive is doing something.

`claim_matrix.py` calls these over a captured transcript, so each one is a pure function of
text -- and can therefore be re-checked here, without a node, from lines a real session
actually recorded. The interesting cases are the negative ones: a reminder the operator had
to ask for twice, a memory that only looked durable because one process answered it, a
health surface that paints everything green. Each of those must *fail* the claim, because a
parser that says "proven" too easily is worse than no parser at all.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import claim_matrix  # noqa: E402  (path set above)

# Lines shaped like the ones the sessions actually wrote.
TIMED = """[SESSION] scenario=timed node=shugo-desktop role=primary-capable
[TURN   ] typed: remind me in two seconds to check the battery
[SPEAK  ] Got it - timer set for 2 seconds.
[AGENT  ] [AGENT] intent: UserIntent(command, conf=0.85)
[AGENT  ] [AGENT] command: timer_set
[SPEAK  ] Timer is done!
[AGENT  ] [AGENT] say: Timer is done!
[AGENT  ] [TIMER] timer fired: Timer (d02e6a90)
[SESSION] speaks=2 early_stop=True
"""

MEMORY = """[SESSION] scenario=memory node=shugo-desktop role=primary-capable
[TURN   ] typed: remember that my sister's name is Ana
[SPEAK  ] I'll remember that.
[SESSION] phase=first: the process ends here
[SESSION] scenario=memory node=shugo-desktop role=primary-capable
[SESSION] phase=second: a new process, the same data dir
[TURN   ] typed: what is my sister's name
[SPEAK  ] My sister's name is Ana.
[AGENT  ] [AGENT] memory recall (question path)
"""

PERSONALITY = """[SESSION] scenario=personality node=shugo-desktop
[AGENT  ] [PERSONALITY] resumed at gen 1
[AGENT  ] [PERSONALITY] governor live (gen 1)
[AGENT  ] [PERSONALITY] grew gen 1 -> 2 (questions 40%; long session (25 turns))
[SESSION] turns=27 (GROWTH_EVERY=25)
"""


class TimedAutonomyTestCase(unittest.TestCase):
    """Did the node act later, on its own, on something it accepted now?"""

    def test_a_goal_acted_on_unprompted_is_proof(self):
        ok, detail = claim_matrix.timer_fired(TIMED)
        self.assertTrue(ok, detail)
        self.assertIn("unprompted", detail)

    def test_a_reminder_the_operator_had_to_repeat_is_not(self):
        text = TIMED.replace("[SPEAK  ] Timer is done!",
                             "[TURN   ] typed: is my timer done?\n[SPEAK  ] Timer is done!")
        ok, detail = claim_matrix.timer_fired(text)
        self.assertFalse(ok, "a repeated reminder passed as autonomy")
        self.assertIn("had to type", detail)

    def test_a_timer_that_fired_silently_is_not(self):
        ok, detail = claim_matrix.timer_fired(TIMED.replace("[SPEAK  ] Timer is done!\n", ""))
        self.assertFalse(ok)
        self.assertIn("said nothing", detail)

    def test_a_timer_that_never_fired_is_not(self):
        text = TIMED.replace("[AGENT  ] [TIMER] timer fired: Timer (d02e6a90)\n", "")
        ok, detail = claim_matrix.timer_fired(text)
        self.assertFalse(ok)
        self.assertIn("no timer ever fired", detail)

    def test_an_empty_session_is_not(self):
        ok, detail = claim_matrix.timer_fired("[SESSION] scenario=timed")
        self.assertFalse(ok)
        self.assertIn("never had a goal accepted", detail)


class MemoryDurabilityTestCase(unittest.TestCase):
    """Did the fact come back in a *new* process, or only in the one that heard it?"""

    def test_a_fact_recalled_after_a_restart_is_proof(self):
        ok, detail = claim_matrix.memory_recalled(MEMORY)
        self.assertTrue(ok, detail)
        self.assertIn("Ana", detail)

    def test_one_process_answering_its_own_memory_is_not(self):
        text = MEMORY.replace(
            "[SESSION] phase=second: a new process, the same data dir\n", "")
        ok, detail = claim_matrix.memory_recalled(text)
        self.assertFalse(ok, "durability passed without a restart")
        self.assertIn("no second process", detail)

    def test_a_forgotten_fact_is_not(self):
        text = MEMORY.replace("My sister's name is Ana.", "I don't know your sister.")
        ok, detail = claim_matrix.memory_recalled(text)
        self.assertFalse(ok)
        self.assertIn("not recalled", detail)

    def test_a_session_that_taught_nothing_is_not(self):
        ok, detail = claim_matrix.memory_recalled(
            MEMORY.replace("[TURN   ] typed: remember that my sister's name is Ana\n", ""))
        self.assertFalse(ok)
        self.assertIn("never taught", detail)


PERCEPTION = """[SESSION] scenario=perception node=shugo-desktop role=primary-capable
[HEARD  ] what is the battery level
[HEARD  ] accepted=True type=speech detail=USER_PRESENT
[AGENT  ] [AGENT] intent: UserIntent(question, conf=0.80)
[SPEAK  ] Battery data isn't available right now.
[AGENT  ] [AGENT] say: Battery data isn't available right now.
"""

STATUS = ('[SESSION] scenario=status node=shugo-desktop\n'
          '[STATUS ] pipeline={"stages": {"sensors": "unknown", "vision": "unknown", '
          '"hearing": "unknown", "speech": "ok", "model": "ok", "memory": "ok"}, '
          '"overall": "unknown", "round_trips": 0}\n')


class HeardCausedActionTestCase(unittest.TestCase):
    """Did words it *heard* do anything, without the operator typing as well?"""

    def test_a_heard_phrase_answered_is_proof(self):
        ok, detail = claim_matrix.heard_caused_action(PERCEPTION)
        self.assertTrue(ok, detail)
        self.assertIn("unprompted", detail)

    def test_a_typed_prompt_beside_the_hearing_is_not(self):
        text = PERCEPTION.replace(
            "[AGENT  ] [AGENT] intent:",
            "[TURN   ] typed: what is the battery level\n[AGENT  ] [AGENT] intent:")
        ok, detail = claim_matrix.heard_caused_action(text)
        self.assertFalse(ok, "a reply to something typed passed as a reply to hearing")
        self.assertIn("typed before the node answered", detail)

    def test_hearing_without_a_reply_is_not(self):
        text = PERCEPTION.replace("[SPEAK  ] Battery data isn't available right now.\n", "")
        ok, detail = claim_matrix.heard_caused_action(text)
        self.assertFalse(ok)
        self.assertIn("said nothing", detail)

    def test_speech_the_node_refused_is_not(self):
        ok, detail = claim_matrix.heard_caused_action(
            PERCEPTION.replace("accepted=True", "accepted=False"))
        self.assertFalse(ok)
        self.assertIn("did not accept", detail)

    def test_a_session_with_nothing_heard_is_not(self):
        ok, detail = claim_matrix.heard_caused_action("[SESSION] scenario=timed")
        self.assertFalse(ok)
        self.assertIn("never gave the node something to hear", detail)


class PipelineHealthTestCase(unittest.TestCase):
    """A health surface that cannot say "unknown" cannot be trusted when it says "ok"."""

    def test_a_snapshot_in_the_house_vocabulary_is_proof(self):
        ok, detail = claim_matrix.pipeline_reported(STATUS)
        self.assertTrue(ok, detail)
        self.assertIn("vision=unknown", detail)
        self.assertIn("overall=unknown", detail)

    def test_an_invented_stage_word_is_not(self):
        ok, detail = claim_matrix.pipeline_reported(
            STATUS.replace('"vision": "unknown"', '"vision": "fine"'))
        self.assertFalse(ok, "a stage outside the vocabulary passed")
        self.assertIn("vocabulary", detail)

    def test_a_missing_organ_is_not(self):
        ok, detail = claim_matrix.pipeline_reported(
            STATUS.replace('"vision": "unknown", ', ""))
        self.assertFalse(ok)
        self.assertIn("organs missing", detail)

    def test_a_snapshot_that_is_not_json_is_not(self):
        ok, detail = claim_matrix.pipeline_reported("[STATUS ] pipeline={broken")
        self.assertFalse(ok)
        self.assertIn("not JSON", detail)

    def test_no_snapshot_at_all_is_not(self):
        ok, detail = claim_matrix.pipeline_reported("[SESSION] scenario=status")
        self.assertFalse(ok)
        self.assertIn("never reported its pipeline", detail)


class ModelBackedTestCase(unittest.TestCase):
    """The claim that separates a node whose reasoning is connected from one whose fallback
    answered -- which is precisely the reading that used to be a lie."""

    def test_a_model_id_as_backing_is_proof(self):
        ok, detail = claim_matrix.model_backed(
            "[TERMINAL]   [MODEL  ] decisions backed by prism-ml/bonsai-27b\n")
        self.assertTrue(ok, detail)
        self.assertIn("prism-ml/bonsai-27b", detail)

    def test_only_rules_backing_is_not_proof(self):
        ok, detail = claim_matrix.model_backed(
            "[TERMINAL]   [MODEL  ] decisions backed by rule_fallback\n"
            "[TERMINAL]   [MODEL  ] decisions backed by none\n")
        self.assertFalse(ok, "a rule-backed node passed the model claim")
        self.assertIn("rather than a model", detail)

    def test_a_transcript_that_announces_nothing_is_not_proof(self):
        ok, detail = claim_matrix.model_backed("[SESSION] scenario=model")
        self.assertFalse(ok)
        self.assertIn("nothing announced", detail)

    def test_no_model_server_at_all_is_unproven_not_contradicted(self):
        """Measured: with nothing listening, this row came out `failed`.

        A claim recorded as contradicted because a service was not running is the
        reading this matrix exists to refuse. The scenario records its own
        precondition, so the parser can read which of the two happened.
        """
        text = ("[SESSION] scenario=model\n"
                "[SESSION] warming the model endpoint at http://127.0.0.1:1234 "
                "(budget 60s)\n"
                "[SESSION] warm-up: NOT warm after 3 attempt(s) in 60s "
                "(model 'x' -> ConnectionError: refused); running anyway\n")
        ok, detail = claim_matrix.model_backed(text)
        self.assertIsNone(ok, detail)
        self.assertIn("no model answered", detail)

    def test_an_endpoint_that_cannot_be_probed_is_unproven(self):
        ok, detail = claim_matrix.model_backed(
            "[SESSION] warm-up: cannot probe the endpoint (ImportError)\n")
        self.assertIsNone(ok, detail)

    def test_a_warm_endpoint_that_announces_nothing_is_still_a_failure(self):
        """The unproven branch must not swallow a session that did run."""
        text = ("[SESSION] warm-up: warm after 1 attempt(s) "
                "(HTTP 200, model shugocore-local)\n"
                "[TERMINAL]   [AGENT  ] cycle=1 outcome=ok\n")
        ok, detail = claim_matrix.model_backed(text)
        self.assertIs(ok, False, "a warmed session that announced nothing is a "
                                 "real failure, not an unevaluated claim")
        self.assertIn("nothing announced", detail)

    def test_a_model_that_could_not_be_reached_fails_the_claim(self):
        text = ("[TERMINAL]   [MODEL  ] decisions backed by shugocore-local\n"
                "[TERMINAL]   [AGENT  ] cycle=1 outcome=BACKEND_FAILURE stages=OBSERVE\n")
        ok, detail = claim_matrix.model_backed(text)
        self.assertFalse(ok, "a cycle that could not reach the model passed")
        self.assertIn("could not reach it", detail)

    def test_the_health_line_counts_too(self):
        ok, detail = claim_matrix.model_backed(
            "  health    model=ok, speech=ok   backed by shugocore-local\n")
        self.assertTrue(ok, detail)


class WorldEngagementTestCase(unittest.TestCase):
    """A world is engaged when a *goal* was expressed, acted on through the gate, and
    answered -- never because the world's interfaces pass their own tests."""

    ENGAGED = ("[WORLD  ] robotics\n"
               "[GOAL   ] pick up the red block\n"
               "[ACTION ] moveit_plan (gated)\n"
               "[REPLY  ] the plan is approved and running\n")

    def test_a_goal_acted_on_and_answered_is_proof(self):
        ok, detail = claim_matrix.world_engagement(self.ENGAGED)
        self.assertTrue(ok, detail)
        self.assertIn("robotics", detail)

    def test_an_ungated_action_is_not_engagement(self):
        ok, detail = claim_matrix.world_engagement(
            self.ENGAGED.replace("(gated)", "(ungated)"))
        self.assertFalse(ok, "an action that bypassed the gate passed as engagement")
        self.assertIn("did not pass the gate", detail)

    def test_an_unanswered_goal_is_not_engagement(self):
        ok, detail = claim_matrix.world_engagement(
            self.ENGAGED.replace("[REPLY  ] the plan is approved and running\n", ""))
        self.assertFalse(ok)
        self.assertIn("never answered", detail)

    def test_a_goal_nobody_acted_on_is_not_engagement(self):
        ok, detail = claim_matrix.world_engagement(
            self.ENGAGED.replace("[ACTION ] moveit_plan (gated)\n", ""))
        self.assertFalse(ok)
        self.assertIn("nothing was done", detail)

    def test_an_absent_transcript_is_unproven_not_contradicted(self):
        """No session recorded is `None` -- could not evaluate -- not `False`.

        `False` reads as "this claim is contradicted", which is what the matrix
        reported for a machine that merely had no headset attached.
        """
        ok, detail = claim_matrix.world_engagement("")
        self.assertIsNone(ok)
        self.assertIn("no world session", detail)

    def test_a_producer_that_says_the_world_never_started_is_unproven(self):
        """The transcript the XR producer writes when its surface cannot start."""
        text = ("world session: 2026-10-06 23:27:56  surface=godot scaffold\n"
                "error=the scaffold printed no world session\n"
                "verdict: world=none presence=unknown acted=False replied=False\n")
        ok, detail = claim_matrix.world_engagement(text)
        self.assertIsNone(ok)
        self.assertIn("world=none", detail)
        self.assertIn("the scaffold printed no world session", detail)

    def test_a_world_that_started_and_failed_is_still_contradicted(self):
        """`None` must not become a blanket excuse for a session that ran."""
        started = ("[WORLD  ] xr\n[GOAL   ] operator: look\n"
                   "[ACTION ] execute_task (gated)\n")
        ok, detail = claim_matrix.world_engagement(started)
        self.assertIs(ok, False, "a session that acted but never answered is a "
                                 "contradiction, not an unevaluated claim")
        self.assertIn("never answered", detail)


class NoThirdPartyEgressTestCase(unittest.TestCase):
    """The privacy claim, read off a transcript rather than asserted in a README."""

    def test_localhost_only_is_proof(self):
        ok, detail = claim_matrix.no_third_party_egress(
            STATUS + "[AGENT  ] [AGENT] agent ready (api=http://127.0.0.1:11434)\n")
        self.assertTrue(ok, detail)
        self.assertIn("no external host", detail)

    def test_an_analytics_host_fails_the_claim(self):
        text = STATUS + ("INFO:backoff:Backing off send_request(...) "
                         "(HTTPSConnectionPool(host='us.i.posthog.com', port=443))\n")
        ok, detail = claim_matrix.no_third_party_egress(text)
        self.assertFalse(ok, "analytics egress passed the privacy claim")
        self.assertIn("posthog", detail)

    def test_an_ordinary_external_host_is_reported_but_not_a_failure(self):
        ok, detail = claim_matrix.no_third_party_egress(
            STATUS + "[AGENT  ] [AGENT] bridge -> https://fleet.example.org/api\n")
        self.assertTrue(ok, detail)
        self.assertIn("fleet.example.org", detail)


class RegistryTestCase(unittest.TestCase):
    """The matrix's own wiring: a live check nobody registered reads as `unproven`."""

    def test_every_claim_live_check_is_registered(self):
        for claim in claim_matrix.CLAIMS:
            for check in claim.get("checks", []):
                if check.get("kind") == "live":
                    self.assertIn(check["name"], claim_matrix.LIVE_CHECKS,
                                  "%s names a live check that does not exist"
                                  % claim["id"])

    def test_every_live_check_is_used_by_a_claim(self):
        used = {check["name"] for claim in claim_matrix.CLAIMS
                for check in claim.get("checks", []) if check.get("kind") == "live"}
        unused = sorted(set(claim_matrix.LIVE_CHECKS) - used)
        self.assertEqual(unused, [], "live checks no claim uses: %s" % unused)

    def test_the_agency_rows_are_in_the_matrix(self):
        ids = {claim["id"] for claim in claim_matrix.CLAIMS}
        for expected in ("agency.timed_autonomy", "agency.memory_durability",
                         "agency.personality_growth", "agency.perception_to_action",
                         "node.pipeline_health", "privacy.no_third_party_egress",
                         "world.desktop"):
            self.assertIn(expected, ids, "%s is not in the matrix" % expected)


class TheVerdictContractTestCase(unittest.TestCase):
    """Every live checker obeys one contract, checked for all of them at once.

    `True` means evidence exists and supports the claim; `False` means evidence
    exists and contradicts it; `None` means the evidence was never produced. The
    third state is the one that gets lost, and losing it is not symmetric: a
    checker that answers an empty transcript with `False` records a claim as
    contradicted because a machine was not set up, and one that answers it with
    `True` marks a claim **proven** because nothing was seen at all.

    Measured before this test existed: `phone_quiet("")` returned
    `True, "no local model calls"` and `no_third_party_egress("")` returned
    `True, "no external host appears in the session at all"` -- so an empty log
    proved `orchestration.top_down` and `privacy.no_third_party_egress`. The matrix
    promises never to mark a claim proven because it is written down; proving one
    from nothing at all is worse.
    """

    BLANKS = ("", "   ", "\n", "\n  \t \n")

    def test_every_checker_reports_unproven_for_no_evidence(self):
        offenders = []
        for name, checker in sorted(claim_matrix.LIVE_CHECKS.items()):
            for blank in self.BLANKS:
                try:
                    result = checker(blank)
                except Exception as exc:      # a crash is not a verdict either
                    offenders.append(f"{name} raised {type(exc).__name__}")
                    continue
                if not (isinstance(result, tuple) and len(result) == 2):
                    offenders.append(f"{name} returned {result!r}, not a tuple")
                elif result[0] is not None:
                    offenders.append(f"{name}({blank!r}) -> {result[0]!r}: "
                                     f"{str(result[1])[:50]}")
        self.assertEqual(
            offenders, [],
            "these checkers judge a claim they have no evidence for -- `None` "
            "(unproven) is the only verdict available with nothing to read:\n  "
            + "\n  ".join(offenders))

    def test_no_checker_proves_a_claim_from_an_empty_transcript(self):
        """The worst case, named separately so a regression is unambiguous."""
        for name in ("phone_quiet", "no_third_party_egress"):
            with self.subTest(checker=name):
                ok, detail = claim_matrix.LIVE_CHECKS[name]("")
                self.assertIsNot(
                    ok, True,
                    f"{name} proved its claim from an empty transcript ({detail})")

    def test_a_checker_still_judges_real_evidence(self):
        """The guard must not swallow the judging: a non-empty transcript counts."""
        ok, detail = claim_matrix.hub_role("role=follower")
        self.assertIs(ok, False, "a status line naming the wrong role must fail")
        ok, detail = claim_matrix.hub_role("  role=primary  ")
        self.assertIs(ok, True, detail)


class TheRegistryCoversEveryLiveCheckTestCase(unittest.TestCase):
    """A claim naming a checker that does not exist would report as `not run`."""

    def test_every_live_check_name_in_the_registry_exists(self):
        missing = []
        for claim in claim_matrix.CLAIMS:
            for check in claim.get("checks", []):
                if check.get("kind") != "live":
                    continue
                name = str(check.get("name"))
                if name not in claim_matrix.LIVE_CHECKS:
                    missing.append(f"{claim['id']} -> {name}")
        self.assertEqual(missing, [],
                         "claims naming a live checker that is not registered:\n  "
                         + "\n  ".join(missing))


if __name__ == "__main__":
    unittest.main()



