"""Thin Robot Framework adapter over the orchestration library.

This file lives outside ``src/`` deliberately. It is the Robot-facing surface, so it is
allowed to know Robot exists; the library it wraps is not (ADR-01-02, ADR-01-08).

Scaffolding only — the keyword vocabulary arrives with M1-11 (issue #46).
"""

from __future__ import annotations

from test_orchestrator import __version__

ROBOT_LIBRARY_SCOPE = "GLOBAL"
ROBOT_LIBRARY_VERSION = __version__


class TestOrchestratorLibrary:
    """Robot library exposing the orchestration keywords.

    ``GLOBAL`` scope, per ADR-01-03's open point closed as OQ-35: tests within one cluster
    do not run in parallel, so run state and ``run_id`` are held once per Robot process.
    """

    ROBOT_LIBRARY_SCOPE = ROBOT_LIBRARY_SCOPE
    ROBOT_LIBRARY_VERSION = ROBOT_LIBRARY_VERSION
