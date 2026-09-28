"""The published package must contain the modules the tree actually has.

`pyproject.toml` enumerates `packages` and `py-modules` by hand, which is what
makes a wheel predictable -- and what makes it silently wrong. The 1.30.23 release
found the list had stopped at 1.30.5: `capabilities.py`, `persona.py`,
`mesh_model_host.py` and nine more were in the repository and absent from anything
installed, so a published wheel could not have run the capability map, phrased a
line, or enabled `fleet_deploy` at all.

This is the packaging twin of `test_android_tree_sync` (which exists because a stale
`DecisionEngine` once shipped to a device): both guard a *derived* artifact against
drifting from its source. Adding a module and forgetting to declare it now fails
here, with the line to add.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Root modules that are deliberately NOT shipped, each for a stated reason. Keep
# this list short and justified: anything absent without an entry here is drift.
NOT_SHIPPED = {
    # A manual harness for a developer's own laptop ("Local test script for
    # ShugoCore on MacBook Air M4"), not part of the runtime.
    "test_local_run": "developer's local smoke script, not runtime",
}

# Directories that hold a package but are deliberately not published.
NOT_SHIPPED_PACKAGES = {
    "tests": "the suite ships in the repository, not in the wheel",
}


def _declared(key):
    """The quoted names in a `key = [...]` array, however it is line-wrapped."""
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(rf"^{key}\s*=\s*\[(.*?)\]", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"pyproject.toml has no {key} array")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _root_modules():
    return {name[:-3] for name in os.listdir(ROOT)
            if name.endswith(".py") and os.path.isfile(os.path.join(ROOT, name))}


def _root_packages():
    return {name for name in os.listdir(ROOT)
            if os.path.isfile(os.path.join(ROOT, name, "__init__.py"))}


class PackagingManifestTestCase(unittest.TestCase):
    def test_every_root_module_is_declared_or_excluded(self):
        declared = _declared("py-modules")
        undeclared = sorted(_root_modules() - declared - set(NOT_SHIPPED))
        self.assertEqual(
            undeclared, [],
            "root modules missing from py-modules in pyproject.toml -- add them so "
            "the published package matches the tree, or list them in NOT_SHIPPED "
            "with a reason:\n  " + "\n  ".join(undeclared))

    def test_every_declared_module_exists(self):
        missing = sorted(_declared("py-modules") - _root_modules())
        self.assertEqual(
            missing, [],
            "pyproject.toml declares modules that are not in the repository: "
            + ", ".join(missing))

    def test_every_package_is_declared_or_excluded(self):
        declared = _declared("packages")
        undeclared = sorted(_root_packages() - declared - set(NOT_SHIPPED_PACKAGES))
        self.assertEqual(
            undeclared, [],
            "packages with an __init__.py missing from pyproject.toml: "
            + ", ".join(undeclared))

    def test_console_scripts_point_at_real_handlers(self):
        """Each entry point must name a module that ships and a function it has."""
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
            text = handle.read()
        block = re.search(r"^\[project\.scripts\](.*?)(?=^\[|\Z)", text,
                          re.S | re.M)
        self.assertIsNotNone(block, "pyproject.toml has no [project.scripts]")
        shipped = _declared("py-modules") | set(NOT_SHIPPED)
        for target in re.findall(r'=\s*"([^"]+)"', block.group(1)):
            module_name, _, attribute = target.partition(":")
            self.assertIn(module_name, shipped,
                          f"entry point {target} names a module that is not shipped")
            with open(os.path.join(ROOT, f"{module_name}.py"), encoding="utf-8") as fh:
                source = fh.read()
            self.assertRegex(
                source, re.compile(rf"^def {re.escape(attribute)}\(", re.M),
                f"entry point {target} names {attribute}(), which "
                f"{module_name}.py does not define at module level")

    def test_changelog_has_a_section_for_the_current_version(self):
        """A release without release notes is one nobody can read afterwards."""
        import version
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertRegex(
            text, re.compile(rf"^## \[{re.escape(version.__version__)}\]", re.M),
            f"CHANGELOG.md has no '## [{version.__version__}]' section")


if __name__ == "__main__":
    unittest.main()