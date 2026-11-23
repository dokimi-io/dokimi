"""A test type's probe mode: what the run-level preflight asks of a plugin.

The preflight's scheduling, image-pull and connectivity checks reuse the first planned
pair rather than a synthetic pod or image. So a plugin that can be preflighted builds
that pair's workload once more, on the same nodes and from the same image, with one
difference: its client is started as a test would be, makes a short exchange with the
server, resolves a cluster name, reports both, and exits instead of measuring.

Kept out of :class:`~dokimi.testtypes.interface.TestTypePlugin`, as ``completion()`` is:
the test-type interface gains it before a second test type. Until then the preflight
fails closed on a plugin that has no probe mode, rather than skip the checks.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from dokimi.testtypes.interface import (
        DeployedWorkload,
        ParameterSchema,
        Placement,
        TestParameters,
        WorkloadSpec,
    )

__all__ = ["PROBE_SECONDS", "ProbeCheck", "ProbeResult", "Probes"]

#: How long a probe's exchange sends for. Every plugin's probe keeps to it, and the
#: preflight budgets the probe pods' deadline and its own wait from it.
PROBE_SECONDS = 1.0


class ProbeCheck(enum.Enum):
    """The checks only a pod can make, spelled as the preflight reports them."""

    CONNECTIVITY = "connectivity"
    DNS = "dns"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """How a probe ended: the check that failed, if any, and what a person reads."""

    failed: ProbeCheck | None
    detail: str

    @property
    def passed(self) -> bool:
        return self.failed is None


@runtime_checkable
class Probes(Protocol):
    """A plugin with a probe mode, as ``iperf2`` has."""

    def parameter_schema(self) -> ParameterSchema: ...

    def probe(self, params: TestParameters, placement: Placement) -> WorkloadSpec:
        """The pair's own workload, with its client in probe mode."""
        ...

    def start(self, deployed: DeployedWorkload) -> None: ...

    def probe_result(self, deployed: DeployedWorkload) -> ProbeResult | None:
        """How the probe ended, or ``None`` while it is still running.

        The probe's only completion signal: a plugin's ``is_complete()`` is about a test,
        and need not understand a probe's report.
        """
        ...
