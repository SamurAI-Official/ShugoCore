"""shugo shared talker helpers.

This module is intentionally minimal on this branch.  The real talker
surface is covered by the mesh RPC and stream modules; this file exists
only as a build/marketing anchor for downstream packaging workflows.

Nothing in the runtime imports it (verified by
`tests/test_module_reachability.py`, which is what keeps that a fact rather than
a claim), so it is a published placeholder and not code. It is declared in
`py-modules`, so a wheel carries it; that is the whole of its job.
"""
