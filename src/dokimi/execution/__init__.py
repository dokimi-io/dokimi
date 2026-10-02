"""Executing a run's tests: phases 5, 6 and 9a (ADR-01-08).

* :mod:`~dokimi.execution.phases` renders the suite, runs one Robot process per pair,
  and merges their reports;
* :mod:`~dokimi.execution.network_test` is what the ``Run Network Test`` keyword does,
  in plain Python;
* :mod:`~dokimi.execution.runner` is the seam to Robot's runner, which is a separate
  process and never an import;
* :mod:`~dokimi.execution.artifacts` and :mod:`~dokimi.execution.context` are the run's
  artifact directory and what each Robot process is told about its run.

Nothing in this package imports Robot Framework (ADR-01-02). Robot is a caller of it.
"""

from __future__ import annotations

from dokimi.execution.artifacts import ARTIFACTS_ROOT, RunArtifacts
from dokimi.execution.context import RunContext
from dokimi.execution.network_test import NetworkTestResult, ToolFailedError, run_network_test
from dokimi.execution.phases import PairExecution, execute_pairs, merge_reports, render_suite
from dokimi.execution.runner import (
    RobotRunError,
    RobotRunner,
    SubprocessRobotRunner,
    default_robot_directory,
)

__all__ = [
    "ARTIFACTS_ROOT",
    "NetworkTestResult",
    "PairExecution",
    "RobotRunError",
    "RobotRunner",
    "RunArtifacts",
    "RunContext",
    "SubprocessRobotRunner",
    "ToolFailedError",
    "default_robot_directory",
    "execute_pairs",
    "merge_reports",
    "render_suite",
    "run_network_test",
]
