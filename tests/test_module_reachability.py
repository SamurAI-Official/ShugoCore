"""Every module in the tree must be reachable, or say why it is not.

`test_packaging_manifest` answers "does the wheel carry what the tree has". This
answers the other half: "is any of it unreachable". A module can ship, pass every
test, and still be code nobody can get to -- and this repository has four of them,
each for a different reason. Measuring that once is a report; measuring it on
every run is a guard, because it is how a *new* unreachable module gets noticed
at the moment it is added instead of a release later.

Kept deliberately blunt: importing, being a console script, or being named as
`<module>.py` somewhere (the way a CLI is documented) all count as reachable.
Anything else must be listed below with a reason *and* a module docstring
explaining itself.
"""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# This guard's own file, which must not be counted as evidence (see `_scan`).
SELF = "tests/test_module_reachability.py"

# Directories that are not the runtime: generated trees, vendored C++, scratch.
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist",
             ".llama_build", "shugocore.egg-info", "runtime", "artifacts",
             "shared", "chroma_db", "platforms", "patches", "simulation"}
SCANNED_EXT = {".py", ".md", ".gradle", ".ps1", ".toml", ".txt", ".yml",
               ".yaml", ".sh"}

# Reachable in no sense: nothing imports it, nothing names it, no console script.
# Keep this short and justified -- the point is that an entry here is a decision
# somebody made, not a place to hide a mistake.
JUSTIFIED_UNREACHABLE = {
    "android_model_manager":
        "deprecated duplicate of model_manager.ModelManager, kept only so the "
        "published 1.x surface still has the module (see its docstring)",
    "talker":
        "published placeholder with no code, by its own docstring; it exists to "
        "be carried by the wheel",
    "test_local_run":
        "a developer's own laptop smoke script, deliberately not shipped",
}

# The same class name defined in two root modules is how a stale copy of a core
# class hides: the live one gets the fix, the other one keeps the old behaviour.
JUSTIFIED_DUPLICATE_CLASSES = {
    "ModelManager":
        "android_model_manager is a deprecated stale copy of "
        "model_manager.ModelManager (and already lacks MODEL_PERFORMANCE_CAP)",
}


def _root_modules():
    return sorted(name[:-3] for name in os.listdir(ROOT)
                  if name.endswith(".py")
                  and os.path.isfile(os.path.join(ROOT, name)))


def _read_source(module):
    with open(os.path.join(ROOT, module + ".py"), encoding="utf-8",
              errors="replace") as handle:
        return handle.read()


def _parse(module):
    try:
        return ast.parse(_read_source(module))
    except SyntaxError:
        return None


def _classes(module):
    """Top-level class names defined in a root module."""
    tree = _parse(module)
    if tree is None:
        return []
    return [n.name for n in tree.body if isinstance(n, ast.ClassDef)]



def _scan():
    """Return (imported_by, mentioned_in) for every root module name."""
    modules = _root_modules()
    imported = {m: set() for m in modules}
    mentioned = {m: set() for m in modules}
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1] not in SCANNED_EXT:
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if rel == SELF:
                # This file's own bookkeeping is not evidence of reachability:
                # its JUSTIFIED_UNREACHABLE reasons name the modules they
                # excuse, which would otherwise make every excuse look stale.
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            for module in modules:
                if rel != module + ".py" and (module + ".py") in text:
                    mentioned[module].add(rel)
            if not name.endswith(".py"):
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif (isinstance(node, ast.ImportFrom) and node.level == 0
                      and node.module):
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for found in names:
                    if found in imported and rel != found + ".py":
                        imported[found].add(rel)
    return imported, mentioned


def _console_script_modules():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as handle:
        text = handle.read()
    block = re.search(r"^\[project\.scripts\](.*?)(?=^\[|\Z)", text, re.S | re.M)
    if block is None:
        return set()
    return {t.split(":")[0] for t in re.findall(r'=\s*"([^"]+)"', block.group(1))}


def _docstring(module):
    tree = _parse(module)
    return (ast.get_docstring(tree) or "") if tree is not None else ""


class ModuleReachabilityTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.imported, cls.mentioned = _scan()
        cls.entries = _console_script_modules()

    def _reachable(self, module):
        return (bool(self.imported[module]) or module in self.entries
                or bool(self.mentioned[module]))

    def test_no_root_module_is_unreachable_without_a_reason(self):
        unexplained = [m for m in _root_modules()
                       if not self._reachable(m) and m not in JUSTIFIED_UNREACHABLE]
        self.assertEqual(
            unexplained, [],
            "these root modules cannot be reached from anywhere (nothing "
            "imports them, nothing names them as a script, no console-script "
            "entry point): " + ", ".join(unexplained)
            + "\nEither wire them up, or add them to JUSTIFIED_UNREACHABLE with "
              "a reason.")

    def test_the_justified_list_has_no_stale_entries(self):
        """A reason for a module that is gone, or no longer needs one."""
        stale = []
        for module, reason in JUSTIFIED_UNREACHABLE.items():
            if not os.path.isfile(os.path.join(ROOT, module + ".py")):
                stale.append(f"{module}: no such module (reason was: {reason})")
            elif self._reachable(module):
                stale.append(f"{module}: reachable now, drop the exception")
        self.assertEqual(stale, [],
                         "stale JUSTIFIED_UNREACHABLE entries:\n  "
                         + "\n  ".join(stale))

    def test_every_justified_module_explains_itself(self):
        """The reason must also live where a reader of the module will see it."""
        missing = [m for m in JUSTIFIED_UNREACHABLE if not _docstring(m).strip()]
        self.assertEqual(
            missing, [],
            "these modules are excused from reachability but carry no module "
            "docstring saying why: " + ", ".join(missing))

    def test_no_duplicate_class_name_without_a_reason(self):
        """Two modules defining the same class is how a stale copy survives."""
        definitions = {}
        for module in _root_modules():
            for name in _classes(module):
                definitions.setdefault(name, []).append(module)
        unexplained = {name: mods for name, mods in definitions.items()
                       if len(mods) > 1
                       and name not in JUSTIFIED_DUPLICATE_CLASSES}
        self.assertEqual(
            unexplained, {},
            "the same class is defined in more than one root module: "
            + "; ".join(f"{n} in {m}" for n, m in sorted(unexplained.items()))
            + "\nConsolidate them, or record why in JUSTIFIED_DUPLICATE_CLASSES.")

    def test_the_duplicate_class_exception_is_still_real(self):
        """Fail once the duplicate is removed, so the note goes with it."""
        for name in JUSTIFIED_DUPLICATE_CLASSES:
            holders = [m for m in _root_modules() if name in _classes(m)]
            self.assertGreater(
                len(holders), 1,
                f"{name} is no longer duplicated ({holders}) -- drop the "
                f"JUSTIFIED_DUPLICATE_CLASSES entry")


if __name__ == "__main__":
    unittest.main()
