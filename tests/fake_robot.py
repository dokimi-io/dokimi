"""A stand-in for Robot Framework's runner, so phase 6 is testable without Robot.

Records every call, and writes the files the real runner would, so that the phases
after it find what they expect. Its contents are placeholders: what Robot actually
writes is tested where Robot is installed (``test_robot_execution.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dokimi.measurement import Measurement, write_measurement
from dokimi.validation import Evaluation, TestError, write_validation


@dataclass
class RunCall:
    suite: Path
    output: Path
    environment: dict[str, str]
    pythonpath: Path


@dataclass
class FakeRobotRunner:
    runs: list[RunCall] = field(default_factory=list)
    merges: list[list[Path]] = field(default_factory=list)
    #: Set to make a pair's process write nothing, as a crashed one would.
    writes_output: bool = True
    #: Called as each pair's process runs: what its test does to the cluster.
    on_run: Callable[[], object] | None = None
    #: What a pair's ``Run Network Test`` read directly from its exporters (phase 7), by
    #: metric name; written next to its output as the real library does. ``None``: none.
    measures: Mapping[str, float] | None = None
    #: What its criteria came to (phase 8), written next to its output as the real
    #: library does. ``None``: nothing, as when its test ended before its criteria.
    validates: Sequence[Evaluation] | None = None
    #: Why its test ended before its criteria, recorded as the real library does.
    ends_with: TestError | None = None

    def run(
        self, suite: Path, *, output: Path, environment: Mapping[str, str], pythonpath: Path
    ) -> None:
        self.runs.append(RunCall(suite, output, dict(environment), pythonpath))
        if self.on_run is not None:
            self.on_run()
        if self.writes_output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("<robot/>", encoding="utf-8")
        if self.measures is not None:
            test_id = output.stem
            labels = {
                "run_id": environment.get("DOKIMI_RUN_ID", ""),
                "test_id": test_id,
                "direction": "inbound",
            }
            write_measurement(
                Measurement(test_id, "inbound", dict(self.measures), labels), output.parent
            )
        if self.ends_with is not None:
            write_validation(output.stem, [], output.parent, error=self.ends_with)
        elif self.validates is not None:
            write_validation(output.stem, self.validates, output.parent)

    def merge(self, outputs: Sequence[Path], *, output: Path, log: Path, report: Path) -> None:
        self.merges.append(list(outputs))
        for path in (output, log, report):
            path.write_text(f"merged from {len(outputs)} output(s)", encoding="utf-8")
