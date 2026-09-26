"""Self-limiting workloads: the ``activeDeadlineSeconds`` every run-owned Pod and Job carries.

[ADR-05-02]: labels make cleanup *possible*, deadlines make it *unnecessary*. An
abandoned ``iperf2`` client that keeps saturating a link is the failure this bounds.

The deadline is derived from the test's own parameters, never a fixed constant — too
tight kills a legitimately slow test, too loose stops bounding anything. And it must
include every wait the workload has to survive after the test itself finishes:
[ADR-03-01] keeps pods alive for the orchestrator's direct read of the exporter and for
the scrape-confirmation window. A deadline that only knew the test duration would kill
the pods during a wait it was never told about, so :class:`DeadlineBudget` makes each of
those waits a required, named term rather than something folded into a margin.

For Jobs the same bound comes with ``backoffLimit: 0`` and ``restartPolicy: Never``
([ADR-01-08] via the Milestone-01 plan): a failed one-shot is a finding, and a Job
controller that silently re-ran it would replace the finding with a second measurement.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, fields

from dokimi.k8s.resources import Manifest

__all__ = [
    "DEFAULT_SAFETY_MARGIN_SECONDS",
    "DeadlineBudget",
    "DeadlineError",
    "bound_job",
    "bound_pod",
]

#: Slack on top of the named terms, for API latency and clock differences. Not a place
#: to hide a wait: anything with a known duration belongs in its own term.
DEFAULT_SAFETY_MARGIN_SECONDS = 30.0


class DeadlineError(ValueError):
    """A deadline could not be derived or applied as asked."""


@dataclass(frozen=True)
class DeadlineBudget:
    """Everything a workload must stay alive for, in seconds."""

    #: The measurement itself, as configured for the test.
    test_duration: float
    #: Scheduling, image pull and the readiness gate before the test starts.
    startup: float
    #: The orchestrator's direct read of the exporter's ``/metrics`` ([ADR-03-01]).
    direct_read: float
    #: The window in which the expected series must reach Prometheus ([ADR-03-01]).
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
            + self.direct_read
            + self.scrape_confirmation
            + self.safety_margin
        )

    @property
    def active_deadline_seconds(self) -> int:
        """The value for ``activeDeadlineSeconds``: whole seconds, rounded up."""
        return math.ceil(self.total)


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
