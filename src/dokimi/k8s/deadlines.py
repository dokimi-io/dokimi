"""Self-limiting workloads: the ``activeDeadlineSeconds`` every run-owned Pod and Job carries.

Labels make cleanup *possible*, deadlines make it *unnecessary*. An
abandoned ``iperf2`` client that keeps saturating a link is the failure this bounds.

The deadline is derived from the test's own parameters, never a fixed constant — too
tight kills a legitimately slow test, too loose stops bounding anything. And it must
include every wait the workload has to survive after the test itself finishes:
pods stay alive for the orchestrator's direct read of the exporter and for
the scrape-confirmation window. A deadline that only knew the test duration would kill
the pods during a wait it was never told about, so :class:`DeadlineBudget` makes each of
those waits a required, named term rather than something folded into a margin — including
the discarded warm-up pass that runs by default, before the measurement.

There are two budgets, of one kind:

* :class:`DeadlineBudget` bounds a **workload**, one test pod or Job;
* :class:`JobBudget` bounds the **orchestrator Job**: every workload budget it runs, plus
  preflight, diagnostics and cleanup. It is always larger than any workload budget it
  contains, and it is also when the run's exclusivity claim expires — by then the cluster
  has killed the Job and every workload of the run has hit its own, shorter deadline.

For Jobs the same bound comes with ``backoffLimit: 0`` and ``restartPolicy: Never``:
a failed one-shot is a finding, and a Job
controller that silently re-ran it would replace the finding with a second measurement.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Sequence
from dataclasses import dataclass, fields

from dokimi.k8s.resources import Manifest
from dokimi.model import TestDefinition

__all__ = [
    "DEFAULT_SAFETY_MARGIN_SECONDS",
    "DeadlineBudget",
    "DeadlineError",
    "JobBudget",
    "bound_job",
    "bound_pod",
    "job_budget_for",
    "workload_budget",
    "workload_budget_for",
]

#: Slack on top of the named terms, for API latency and clock differences. Not a place
#: to hide a wait: anything with a known duration belongs in its own term.
DEFAULT_SAFETY_MARGIN_SECONDS = 30.0

# Milestone-01 defaults for the terms a definition does not state. Each is generous, not
# tight: these bounds exist to stop an orphaned run, not to enforce a schedule.
#: Scheduling, image pull on a cold node and the readiness gate.
DEFAULT_STARTUP_SECONDS = 300.0
#: The discarded warm-up pass: ``iperf2``'s default ``warmup`` parameter
#: (:mod:`dokimi.testtypes.iperf2`), and what a budget reserves when not told otherwise.
DEFAULT_WARMUP_SECONDS = 10.0
DEFAULT_DIRECT_READ_SECONDS = 30.0
#: A few scrape intervals at the common 15 to 30 s.
DEFAULT_SCRAPE_CONFIRMATION_SECONDS = 120.0
#: Phase 4, every wait in it at its longest: the Prometheus query check (180 s), the probe
#: pair's startup (:data:`DEFAULT_STARTUP_SECONDS`), its start and exchange (90 s) and its
#: removal (60 s). ``tests/test_preflight.py`` holds it to the timeouts themselves.
DEFAULT_PREFLIGHT_SECONDS = 630.0
DEFAULT_DIAGNOSTICS_SECONDS = 120.0
DEFAULT_CLEANUP_SECONDS = 120.0


class DeadlineError(ValueError):
    """A deadline could not be derived or applied as asked."""


@dataclass(frozen=True)
class DeadlineBudget:
    """Everything a workload must stay alive for, in seconds."""

    #: The measurement itself, as configured for the test.
    test_duration: float
    #: Scheduling, image pull and the readiness gate before the test starts.
    startup: float
    #: The discarded warm-up pass before the measurement; ``0`` when disabled.
    warmup: float
    #: The orchestrator's direct read of the exporter's ``/metrics``.
    direct_read: float
    #: The window in which the expected series must reach Prometheus.
    scrape_confirmation: float
    safety_margin: float = DEFAULT_SAFETY_MARGIN_SECONDS

    def __post_init__(self) -> None:
        for term in fields(self):
            value = getattr(self, term.name)
            if not math.isfinite(value) or value < 0:
                raise DeadlineError(f"deadline term {term.name} must be >= 0, got {value!r}")
        if self.test_duration <= 0:
            raise DeadlineError("a workload deadline needs a positive test duration")

    @property
    def total(self) -> float:
        return (
            self.test_duration
            + self.startup
            + self.warmup
            + self.direct_read
            + self.scrape_confirmation
            + self.safety_margin
        )

    @property
    def active_deadline_seconds(self) -> int:
        """The value for ``activeDeadlineSeconds``: whole seconds, rounded up."""
        return math.ceil(self.total)


@dataclass(frozen=True)
class JobBudget:
    """Everything the orchestrator Job must stay alive for, in seconds."""

    #: One workload budget per pair, in the order the concurrency plan runs them.
    workloads: Sequence[DeadlineBudget]
    preflight: float
    diagnostics: float
    cleanup: float
    safety_margin: float = DEFAULT_SAFETY_MARGIN_SECONDS

    def __post_init__(self) -> None:
        if not self.workloads:
            raise DeadlineError("a Job budget needs at least one workload")
        for name in ("preflight", "diagnostics", "cleanup", "safety_margin"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise DeadlineError(f"Job budget term {name} must be >= 0, got {value!r}")

    @property
    def total(self) -> float:
        # Sequential execution in Milestone-01: pairs run one after another. Bounded
        # concurrency will sum per wave instead; how to bound that at submission is open.
        return (
            sum(workload.total for workload in self.workloads)
            + self.preflight
            + self.diagnostics
            + self.cleanup
            + self.safety_margin
        )

    @property
    def active_deadline_seconds(self) -> int:
        """The value for the Job's ``activeDeadlineSeconds``: whole seconds, rounded up."""
        return math.ceil(self.total)


def workload_budget_for(
    definition: TestDefinition, *, warmup_seconds: float = DEFAULT_WARMUP_SECONDS
) -> DeadlineBudget:
    """The workload budget for *definition*'s pair, from its duration and the defaults.

    *warmup_seconds* is the pass the workload actually runs — ``0`` when it is disabled —
    so a workload is never budgeted for a warm-up it does not do, nor denied one it does.
    """
    return workload_budget(float(definition.duration.seconds), warmup_seconds=warmup_seconds)


def workload_budget(
    test_duration_seconds: float, *, warmup_seconds: float = DEFAULT_WARMUP_SECONDS
) -> DeadlineBudget:
    """The workload budget for one pair that measures for *test_duration_seconds*.

    What phase 6 uses, from the parameters it runs with rather than from a definition,
    and what :func:`workload_budget_for` uses, so the two cannot budget differently.
    """
    return DeadlineBudget(
        test_duration=test_duration_seconds,
        startup=DEFAULT_STARTUP_SECONDS,
        warmup=warmup_seconds,
        direct_read=DEFAULT_DIRECT_READ_SECONDS,
        scrape_confirmation=DEFAULT_SCRAPE_CONFIRMATION_SECONDS,
    )


def job_budget_for(definition: TestDefinition) -> JobBudget:
    """The orchestrator Job's budget for *definition*.

    Computed at submission, before the Job plans anything. Milestone-01 has exactly one
    pair, so the plan follows from the definition; general matrices need an upper bound
    on the plan instead.
    """
    return JobBudget(
        workloads=(workload_budget_for(definition),),
        preflight=DEFAULT_PREFLIGHT_SECONDS,
        diagnostics=DEFAULT_DIAGNOSTICS_SECONDS,
        cleanup=DEFAULT_CLEANUP_SECONDS,
    )


def bound_pod(manifest: Manifest, budget: DeadlineBudget) -> Manifest:
    """A copy of a Pod *manifest* that stops itself after *budget*.

    ``restartPolicy`` defaults to ``Never``: a restarted test container would silently
    produce a second measurement under the same identity. An explicit value is kept.
    """
    _expect_kind(manifest, "Pod")
    bounded = copy.deepcopy(manifest)
    spec = bounded.setdefault("spec", {})
    spec["activeDeadlineSeconds"] = budget.active_deadline_seconds
    spec.setdefault("restartPolicy", "Never")
    return bounded


def bound_job(manifest: Manifest, budget: DeadlineBudget) -> Manifest:
    """A copy of a Job *manifest* that stops itself after *budget* and never retries."""
    _expect_kind(manifest, "Job")
    bounded = copy.deepcopy(manifest)
    spec = bounded.setdefault("spec", {})
    spec["activeDeadlineSeconds"] = budget.active_deadline_seconds

    if spec.get("backoffLimit", 0) != 0:
        raise DeadlineError(
            f"Job backoffLimit must be 0 for a run-owned workload, got {spec['backoffLimit']!r}"
        )
    spec["backoffLimit"] = 0

    pod_spec = spec.setdefault("template", {}).setdefault("spec", {})
    if pod_spec.get("restartPolicy", "Never") != "Never":
        raise DeadlineError(
            f"Job pods must use restartPolicy Never, got {pod_spec['restartPolicy']!r}"
        )
    pod_spec["restartPolicy"] = "Never"
    return bounded


def _expect_kind(manifest: Manifest, kind: str) -> None:
    if manifest.get("kind") != kind:
        raise DeadlineError(f"expected a {kind} manifest, got {manifest.get('kind')!r}")
