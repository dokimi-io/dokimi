"""Phases 5, 6 and 9a: render the suite, run it per pair, merge the reports ([ADR-01-08]).

Each phase is its own function over the run's artifact directory, so that the phase
boundaries stay an interface between resumable stages ([ADR-01-08], OQ-99) rather than
seams inside one call: a later run could start at phase 6 from a retained suite.

Nothing that must happen once per run lives inside Robot. Phase 6 starts one process
per pair, and the work around them, rendering before and merging after, is here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dokimi.execution.artifacts import RunArtifacts
from dokimi.execution.context import RunContext
from dokimi.execution.runner import RobotRunError, RobotRunner
from dokimi.formats import render_robot
from dokimi.model import TestDefinition
from dokimi.planning import Plan, PlannedPair
from dokimi.testtypes.interface import WorkloadImage

__all__ = ["PairExecution", "execute_pairs", "merge_reports", "render_suite"]


@dataclass(frozen=True, slots=True)
class PairExecution:
    """One pair's Robot process: which pair, and where its results are."""

    pair: PlannedPair
    output: Path


def render_suite(definition: TestDefinition, artifacts: RunArtifacts) -> Path:
    """Phase 5: write the suite that is executed, and keep it ([ADR-01-08], OQ-100).

    ``render_robot()`` is the same function the user-facing conversion command calls,
    so what runs is byte for byte what ``dokimi convert`` shows ([ADR-04-05]).
    """
    artifacts.create()
    artifacts.suite.write_text(render_robot(definition), encoding="utf-8")
    return artifacts.suite


def execute_pairs(
    plan: Plan,
    *,
    suite: Path,
    artifacts: RunArtifacts,
    runner: RobotRunner,
    robot_directory: Path,
    namespace: str,
    cluster: str,
    images: Mapping[str, WorkloadImage],
    on_pair: Callable[[PlannedPair], None] = lambda _pair: None,
    after_pair: Callable[[PlannedPair], None] = lambda _pair: None,
) -> list[PairExecution]:
    """Phase 6: one Robot process per pair, in the plan's order.

    Milestone-01's plan is one pair, so this runs once. It is still the general shape:
    the matrix growing past one pair is Milestone-02, and concurrency within a wave is
    the scheduler's to add here (ADR-04-04), not Robot's.

    *on_pair* is called before a pair's process starts, *after_pair* once it has ended,
    however it ended: phase 10 asks then whether the pair's pods were deployed.

    Raises:
        RobotRunError: if a pair's process could not run, or wrote no results.
    """
    executions = []
    for wave in plan.waves:
        for pair in wave:
            on_pair(pair)
            output = artifacts.pair_output(pair.test_id)
            context = RunContext(
                artifacts.run_id,
                namespace,
                pair.test_id,
                pair.source.name,
                pair.target.name,
                cluster,
                images,
            )
            try:
                runner.run(
                    suite,
                    output=output,
                    environment=context.environment(),
                    pythonpath=robot_directory,
                )
            finally:
                after_pair(pair)
            if not output.is_file():
                raise RobotRunError(f"{pair.test_id}: Robot Framework wrote no {output.name}")
            executions.append(PairExecution(pair, output))
    return executions


def merge_reports(
    executions: Sequence[PairExecution], artifacts: RunArtifacts, runner: RobotRunner
) -> RunArtifacts:
    """Phase 9a: every pair's ``output.xml``, merged into the run's one report.

    With one pair the merge is a pass-through, and it runs anyway: building the one-pair
    case as if it were the only case would make the n-pair one a retrofit ([ADR-01-08]).
    """
    runner.merge(
        [execution.output for execution in executions],
        output=artifacts.output,
        log=artifacts.log,
        report=artifacts.report,
    )
    return artifacts
