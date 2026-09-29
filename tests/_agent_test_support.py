"""Isolated agent construction, so a test never writes into the repo.

Every agent builds its own semantic memory at ``data_dir/semantic_memory.db``. With no
``data_dir`` that file lands in the working directory -- which, during the suite, is the repo
-- and the teardown that deletes it then fails on Windows while the connection is still open:
``PermissionError: [WinError 32] ... 'semantic_memory.db'``. A passing test reports as a
failure, which is how this went unnoticed across five test modules at once.

Tests take a throwaway directory instead. Cleanup is deliberately best-effort: the directory
belongs to the test rather than to the repo, so a file still open at that point is not
something to fail a run over.
"""
import shutil
import tempfile


def isolated_data_dir(case) -> str:
    """A per-test data directory, removed after the test's own teardown has run.

    ``addCleanup`` callbacks run after ``tearDown``, so this cannot race the agent's own
    shutdown.
    """
    path = tempfile.mkdtemp(prefix="shugo_test_")
    case.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path
