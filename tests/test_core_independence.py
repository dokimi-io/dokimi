"""The core library must import and pass its tests with Robot Framework absent.

ADR-01-02 requires the orchestration library to stay usable without Robot Framework, and
ADR-01-08 makes Robot a runtime dependency of a *run* rather than of the library. That is
easy to state and easy to break by accident — one stray import in a module everything
else pulls in is enough.

This is the guard that keeps the property real. The import check runs in a subprocess so
the result cannot be contaminated by whatever the rest of the test session has already
imported.
"""

from __future__ import annotations

import subprocess
import sys

CORE_MODULES = [
    "dokimi",
    "dokimi.cleanup",
    "dokimi.cli",
    "dokimi.diagnostics",
    "dokimi.formats",
    "dokimi.k8s",
    "dokimi.k8s.cluster",
    "dokimi.k8s.deadlines",
    "dokimi.k8s.exclusivity",
    "dokimi.k8s.namespaces",
    "dokimi.k8s.ownership",
    "dokimi.k8s.resources",
    "dokimi.metrics",
    "dokimi.metrics.managed",
    "dokimi.metrics.prometheus",
    "dokimi.model",
    "dokimi.planning",
    "dokimi.preflight",
    "dokimi.results",
    "dokimi.run_identity",
    "dokimi.testtypes",
    "dokimi.testtypes.images",
    "dokimi.testtypes.interface",
    "dokimi.testtypes.iperf2",
    "dokimi.testtypes.lifecycle",
    "dokimi.validation",
]

_PROBE = f"""
import importlib, sys

for name in {CORE_MODULES!r}:
    importlib.import_module(name)

leaked = sorted(n for n in sys.modules if n == "robot" or n.startswith("robot."))
print(",".join(leaked))
"""


def test_core_import_does_not_pull_in_robot_framework() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    leaked = [name for name in result.stdout.strip().split(",") if name]
    assert not leaked, (
        f"importing the core library pulled in Robot Framework modules: {leaked}. "
        "The core must stay importable with Robot Framework not installed (ADR-01-02)."
    )
