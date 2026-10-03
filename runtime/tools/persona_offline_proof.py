#!/usr/bin/env python3
"""Run the persona suite with every outbound connection refused.

These tests used to pass only because this fleet's LAN happened to answer at
192.168.1.162:11434 -- the Mac's Ollama -- and failed on CI, which is nowhere near it.
Passing here, with sockets refusing and urlopen raising, is the evidence that they test
the *rule* (adopt a live locator, never adopt a dead one, always keep the draft) rather
than whether a particular machine is awake.

    py -3.10 runtime/probe/persona_offline_proof.py
"""
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

seen = {"probes": 0}


def refused(*_args, **_kwargs):
    seen["probes"] += 1
    raise OSError("offline-proof: connection refused")


def no_route(*_args, **_kwargs):
    seen["probes"] += 1
    raise OSError("offline-proof: no route to host")


# capabilities._resolve_addr defaults to socket.create_connection; persona._first_model
# uses urllib.request.urlopen. Both are looked up at call time, so patching the module
# attributes is enough -- and anything that slips past these two would still be caught,
# because nothing else in the suite opens a connection.
socket.create_connection = refused
import urllib.request  # noqa: E402

urllib.request.urlopen = no_route

suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern="test_persona.py")
result = unittest.TextTestRunner(verbosity=1).run(suite)
print(f"\noffline: {seen['probes']} outbound attempt(s) refused by the harness")
print("hermetic:", result.wasSuccessful())
sys.exit(0 if result.wasSuccessful() else 1)