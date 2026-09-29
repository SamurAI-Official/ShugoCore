"""A throwaway working directory per test.

Several suites build real agents in-process. An agent writes its semantic memory to
``data_dir/semantic_memory.db`` -- relative to the working directory when no ``data_dir`` is
passed -- so with the suite run from the repo those files land in the repo, and the teardown
that removes them fails on Windows while the connection is still open:

    PermissionError: [WinError 32] ... 'semantic_memory.db'

A passing test then reports as a failure, which is how the same defect went unnoticed across
five modules at once (48 failures). Each test now runs in its own directory, which pytest
removes afterwards, and nothing the suite does can leave state in the repo.

Tests that need repo files resolve them from ``__file__`` (as the sound suites do) rather than
assuming the working directory.
"""
import pytest


@pytest.fixture(autouse=True)
def clean_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    yield tmp_path
