"""Every `tests/test_*.py` must actually be collectable.

The suite needs two runners (`ARCHITECTURE.md` explains why), and that is exactly
the setup where a file can look like a test and never run. Measuring the split
once was a report; this is the guard.

A `test_*.py` that defines neither a `TestCase` subclass nor a module-level
`test*` function cannot be collected by *either* runner: it is a file whose name
promises tests that do not exist. A module-level `pytest.importorskip` counts, so
a file that skips until its models are fetched is fine and stays green here.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

# test_*.py files that are deliberate shims rather than suites. Keep short.
NOT_A_SUITE = {}


def _test_files():
    return sorted(name for name in os.listdir(TESTS)
                  if name.startswith("test_") and name.endswith(".py")
                  and os.path.isfile(os.path.join(TESTS, name)))


def _derives_from_testcase(node):
    for base in node.bases:
        if isinstance(base, ast.Attribute) and base.attr == "TestCase":
            return True
        if isinstance(base, ast.Name) and base.id == "TestCase":
            return True
        # A shared base of our own, e.g. class T(BaseTestCase)
        if isinstance(base, ast.Name) and "Test" in base.id:
            return True
    return False


def _has_test_method(node):
    """A class with any `test*` method is a suite, whatever it inherits.

    Relying on the base name alone missed classes built on a local base (the
    provider contract suite is one), and pytest collects a class from its methods
    in any case.
    """
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name.startswith("test"):
                return True
    return False


def _classify(name):
    """What this file offers a runner, or None when it offers nothing."""
    with open(os.path.join(TESTS, name), encoding="utf-8", errors="replace") as fh:
        try:
            tree = ast.parse(fh.read())
        except SyntaxError as exc:
            return f"syntax error: {exc}"
    cases = [n for n in tree.body if isinstance(n, ast.ClassDef)
             and (_derives_from_testcase(n) or _has_test_method(n))]
    funcs = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name.startswith("test")]
    if cases and funcs:
        return "both"
    if cases:
        return "unittest"
    if funcs:
        return "pytest"
    # A file that only imports and re-exports helpers is not a suite.
    return None


class EveryTestFileIsCollectableTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.files = _test_files()

    def test_there_are_test_files_to_check(self):
        self.assertGreater(len(self.files), 50,
                           "tests/ looks empty -- check the path")

    def test_no_test_named_file_is_uncollectable(self):
        silent = []
        for name in self.files:
            if name in NOT_A_SUITE:
                continue
            if _classify(name) is None:
                silent.append(name)
        self.assertEqual(
            silent, [],
            "these files are named test_* but define neither a TestCase subclass "
            "nor a module-level test function, so NEITHER runner collects them "
            "-- their tests silently do not run:\n  " + "\n  ".join(silent)
            + "\nFix them, rename them, or record why in NOT_A_SUITE.")

    def test_a_recorded_exception_is_still_needed(self):
        """Fail when an excused file is fixed, so the note goes with it."""
        stale = [name for name in NOT_A_SUITE
                 if name not in self.files or _classify(name) is not None]
        self.assertEqual(stale, [],
                         "stale NOT_A_SUITE entries (the file is gone or "
                         "collectable now -- drop the exception):\n  "
                         + "\n  ".join(stale))

    def test_the_tests_directory_is_importable(self):
        """`tests.<module>` imports are how the smoke fragments share helpers."""
        self.assertTrue(
            os.path.isfile(os.path.join(TESTS, "__init__.py")),
            "tests/__init__.py is missing: cross-module imports like "
            "`from tests.android_device_smoke import ...` stop resolving")


if __name__ == "__main__":
    unittest.main()
