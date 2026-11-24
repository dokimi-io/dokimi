"""Robot Framework's runner and ``rebot``, as processes the library starts.

Robot is a runtime dependency of a run, in the orchestrator's image, but never a
dependency of this library: nothing here imports it. The runner is a separate Python
process, ``python -m robot``, and a run starts **one
Robot process per test pair**, merged by ``rebot`` into one ``log.html`` for the run.

:class:`RobotRunner` is the seam. :class:`SubprocessRobotRunner` is the real one; the
core test suite uses a fake, and passes with Robot Framework not installed.

**Robot's exit code is never the run's.** It counts failed tests, and 250 and above mean
Robot itself could not run. The first is read from the results, not from here; the
second raises :class:`RobotRunError`, because then no report can be trusted.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

__all__ = [
    "ROBOT_DIRECTORY_VARIABLE",
    "RobotRunError",
    "RobotRunner",
    "SubprocessRobotRunner",
    "default_robot_directory",
]

#: Where the orchestrator image keeps the Robot resources and library adapter.
ROBOT_DIRECTORY_VARIABLE = "DOKIMI_ROBOT_DIR"

#: Robot's own codes for "I could not run", as opposed to "N tests failed".
_ROBOT_CANNOT_RUN = {
    251: "help or version output was requested",
    252: "invalid data or command line options",
    253: "execution was interrupted",
    255: "an unexpected internal error",
}


class RobotRunError(RuntimeError):
    """Robot Framework or ``rebot`` could not run at all, so its results are not results."""


class RobotRunner(Protocol):
    """Runs a suite for one pair, and merges every pair's results into one report."""

    def run(
        self, suite: Path, *, output: Path, environment: Mapping[str, str], pythonpath: Path
    ) -> None:
        """Execute *suite*, writing its results to *output* and nothing else."""
        ...

    def merge(self, outputs: Sequence[Path], *, output: Path, log: Path, report: Path) -> None:
        """Merge *outputs* into one ``output.xml``, ``log.html`` and ``report.html``."""
        ...


class SubprocessRobotRunner:
    """The real runner: ``python -m robot`` per pair, ``python -m robot.rebot`` once."""

    def __init__(self, python: str = sys.executable) -> None:
        self._python = python

    def run(
        self, suite: Path, *, output: Path, environment: Mapping[str, str], pythonpath: Path
    ) -> None:
        # Never read a stale file as this run's results: a process that dies before writing
        # must show up as "wrote nothing".
        output.unlink(missing_ok=True)
        # The per-pair process writes only output.xml; the run's one log.html is rebot's.
        self._call(
            "robot",
            [
                "--output",
                str(output),
                "--log",
                "NONE",
                "--report",
                "NONE",
                "--pythonpath",
                str(pythonpath),
                "--consolecolors",
                "off",
                str(suite),
            ],
            environment,
        )

    def merge(self, outputs: Sequence[Path], *, output: Path, log: Path, report: Path) -> None:
        self._call(
            "robot.rebot",
            [
                "--output",
                str(output),
                "--log",
                str(log),
                "--report",
                str(report),
                *(str(path) for path in outputs),
            ],
            {},
        )

    def _call(self, module: str, arguments: list[str], environment: Mapping[str, str]) -> None:
        command = [self._python, "-m", module, *arguments]
        # The Job's own environment, plus the pair's: the Robot process inherits the
        # cluster credentials the Job has, and is told which pair it is.
        completed = subprocess.run(  # noqa: S603 - our interpreter, our arguments
            command, env={**os.environ, **environment}, check=False
        )
        code = completed.returncode
        if code < 0:
            raise RobotRunError(f"`python -m {module}` was killed by signal {-code}")
        # 0 to 250 count failed tests, which the results say better. Above is Robot itself.
        if code > 250:
            meaning = _ROBOT_CANNOT_RUN.get(code, "an exit code Robot Framework does not document")
            raise RobotRunError(f"`python -m {module}` stopped with {code}: {meaning}")


def default_robot_directory() -> Path:
    """Where the Robot resources and the library adapter are.

    ``DOKIMI_ROBOT_DIR`` in the orchestrator image. Otherwise the repository's own
    ``robot/`` directory, which is where it is in a source checkout.
    """
    configured = os.environ.get(ROBOT_DIRECTORY_VARIABLE, "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "robot"
