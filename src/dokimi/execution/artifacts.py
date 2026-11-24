"""Where a run's artifacts are written inside the Job: one directory per ``run_id``.

A run retains the rendered suite in its artifact directory, keyed by
``run_id``, next to one ``output.xml`` per pair and the merged ``log.html`` and
``report.html``. This is that directory's layout, and the only place that knows it, so
the transport that returns it to the submitter and M1-14's
result artifact write into the same place.

Inside the Job it lives under ``/work``, the only writable volume of a container whose
root filesystem is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["ARTIFACTS_ROOT", "RunArtifacts"]

#: The Job's writable working volume, where every run's artifact directory lives.
ARTIFACTS_ROOT = Path("/work/artifacts")


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """One run's artifact directory and the files in it."""

    root: Path
    run_id: str

    @property
    def directory(self) -> Path:
        return self.root / self.run_id

    @property
    def suite(self) -> Path:
        """Phase 5's output: the exact suite Robot executed, so ``robot <it>`` reruns it."""
        return self.directory / "suite.robot"

    def pair_output(self, test_id: str) -> Path:
        """One pair's ``output.xml``, as its Robot process writes it (phase 9a)."""
        return self.directory / "pairs" / f"{test_id}.xml"

    @property
    def output(self) -> Path:
        """Every pair's results, merged by ``rebot``."""
        return self.directory / "output.xml"

    @property
    def log(self) -> Path:
        return self.directory / "log.html"

    @property
    def report(self) -> Path:
        return self.directory / "report.html"

    def create(self) -> RunArtifacts:
        """Create the directory and its ``pairs/`` subdirectory. Returns itself."""
        (self.directory / "pairs").mkdir(parents=True, exist_ok=True)
        return self
