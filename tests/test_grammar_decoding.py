"""Grammar-constrained decoding: GBNF decision grammar + backend plumbing.

The on-device 0.5B model used to emit loose dialects ("action_type: speak"
with no braces, or "record_observation: {json}") that _parse_proposal could
not read, so every decision fell back to rules. The decision path now sends a
GBNF grammar that pins the proposal shape; these tests cover the grammar
builder and that each backend receives/forwards the constraint.
"""
import unittest
from unittest import mock

from android_inference import AndroidBackend
import model_backends
from model_backends import OllamaBackend
from subconscious import SubconsciousModel, build_decision_grammar

SCHEMA = ["speak", "ask_user", "record_observation"]


class TestDecisionGrammar(unittest.TestCase):
    def test_empty_schema_yields_no_grammar(self):
        self.assertEqual(build_decision_grammar([]), "")
        self.assertEqual(build_decision_grammar(None), "")

    def test_null_only_schema_yields_no_grammar(self):
        self.assertEqual(build_decision_grammar(["null"]), "")

    def test_every_action_type_and_null_are_allowed(self):
        grammar = build_decision_grammar(SCHEMA)
        for name in SCHEMA:
            self.assertIn('"\\"%s\\""' % name, grammar, name)
        self.assertIn('"\\"null\\""', grammar)

    def test_duplicates_collapse(self):
        grammar = build_decision_grammar(["speak", "speak", " speak "])
        self.assertEqual(grammar.count('"\\"speak\\""'), 1)

    def test_grammar_requires_the_proposal_keys(self):
        grammar = build_decision_grammar(SCHEMA)
        for key in ("action_type", "params", "confidence", "text"):
            self.assertIn('\\"%s\\"' % key, grammar, key)
        for rule in ("root", "action", "params", "value", "string",
                     "number", "ws"):
            self.assertIn("\n%s " % rule, "\n" + grammar, rule)

    def test_grammar_root_starts_with_an_object(self):
        grammar = build_decision_grammar(SCHEMA)
        root = [ln for ln in grammar.splitlines() if ln.startswith("root")][0]
        self.assertIn('"{"', root)
        self.assertIn('"}"', root)
        # Root must END at the closing brace: a trailing `ws` would let the
        # model pad after the object instead of completing the grammar.
        self.assertFalse(root.rstrip().endswith("ws"), root)


class TestAndroidBackendGrammar(unittest.TestCase):
    def test_grammar_is_sent_when_provided(self):
        backend = AndroidBackend(api_url="http://127.0.0.1:11434")
        with mock.patch("android_inference._http_post",
                        return_value={"response": "ok"}) as post:
            out = backend.generate("shugocore-local", "prompt",
                                   grammar='root ::= "a"')
        self.assertEqual(out, "ok")
        self.assertEqual(post.call_args.args[1]["grammar"], 'root ::= "a"')

    def test_grammar_is_omitted_when_absent(self):
        backend = AndroidBackend(api_url="http://127.0.0.1:11434")
        with mock.patch("android_inference._http_post",
                        return_value={"response": "ok"}) as post:
            backend.generate("shugocore-local", "prompt")
        self.assertNotIn("grammar", post.call_args.args[1])

    def test_chat_forwards_grammar_too(self):
        backend = AndroidBackend(api_url="http://127.0.0.1:11434")
        with mock.patch("android_inference._http_post",
                        return_value={"message": {"content": "hi"}}) as post:
            backend.chat("shugocore-local", [{"role": "user", "content": "x"}],
                         grammar="g")
        self.assertEqual(post.call_args.args[1]["grammar"], "g")


class TestHostBackendGrammarMapping(unittest.TestCase):
    """Backends without GBNF map the constraint to their JSON mode."""

    def _post_body(self, **kwargs):
        fake = mock.MagicMock()
        with mock.patch.object(model_backends.requests, "post",
                               return_value=fake) as post, \
                mock.patch.object(model_backends, "_check_response"), \
                mock.patch.object(model_backends, "_read_bounded",
                                  return_value=b'{"response": "hi"}'):
            OllamaBackend().generate("shugocore-local", "p", **kwargs)
            return post.call_args.kwargs["json"]

    def test_ollama_sets_format_json_with_grammar(self):
        self.assertEqual(self._post_body(grammar="g").get("format"), "json")

    def test_ollama_leaves_format_unset_without_grammar(self):
        self.assertNotIn("format", self._post_body())


class TestDecisionPathWiring(unittest.TestCase):
    """Grammar is applied to decisions, never to conversational speech."""

    class _Backend:
        def __init__(self):
            self.seen = []

        def list_models(self):
            return []

        def generate(self, model_id, prompt, timeout=None, grammar=None):
            self.seen.append(grammar)
            return "{}"

    def test_decision_passes_grammar_conversation_does_not(self):
        backend = self._Backend()
        model = SubconsciousModel(backend=backend)

        model.get_model_output("shugocore-local", {"content": "hi"},
                               action_schema=SCHEMA)
        self.assertTrue(backend.seen[-1])
        self.assertIn('"\\"speak\\""', backend.seen[-1])

        model.get_conversational_output("shugocore-local", "hello there")
        self.assertIsNone(backend.seen[-1])

    def test_backend_without_grammar_kwarg_still_works(self):
        """Pre-grammar backends and doubles must not raise TypeError."""
        calls = []

        class _LegacyBackend:
            def list_models(self):
                return []

            def generate(self, model_id, prompt, timeout=None):
                calls.append((model_id, timeout))
                return "{}"

        model = SubconsciousModel(backend=_LegacyBackend())
        out = model.get_model_output("shugocore-local", {"content": "hi"},
                                     action_schema=SCHEMA)
        self.assertEqual(out, "{}")
        self.assertEqual(len(calls), 1)

    def test_grammar_capability_probe_is_cached(self):
        backend = self._Backend()
        model = SubconsciousModel(backend=backend)
        model.get_model_output("shugocore-local", {"content": "a"},
                               action_schema=SCHEMA)
        cache = model._grammar_support_cache
        self.assertEqual(list(cache), [type(backend)])
        self.assertTrue(cache[type(backend)])


class TestDecisionPromptSteering(unittest.TestCase):
    """The decision prompt biases routine ticks to the safe action."""

    def test_prompt_prefers_record_observation_and_flags_side_effects(self):
        from subconscious import _build_decision_prompt

        prompt = _build_decision_prompt(
            '{"content": "hi"}', ["record_observation", "network_send"])
        self.assertIn("record_observation", prompt)
        self.assertIn("network_send", prompt)
        self.assertIn("Prefer it for routine self-maintenance", prompt)
        self.assertIn("need operator approval", prompt)


if __name__ == "__main__":
    unittest.main()
