"""What ``Run Network Test`` does, in plain Python ([ADR-01-08] phase 6).

The Robot keyword is a thin adapter over :func:`run_network_test`. Robot supplies the
control flow of an already-declarative suite and the report. This function supplies
everything else, so a caller of the library without Robot runs exactly the same steps:

1. the test type's plugin, with the image ``submit`` resolved for it ([ADR-04-08]);
2. its parameters, validated by its own schema ([ADR-02-01]);
3. the pair's pods, created run-owned and bounded by a deadline ([ADR-05-02]);
4. the per-test readiness gate, then ``start()`` ([ADR-05-07]);
5. waiting for completion through the plugin, never by reading a pod directly. For
   ``iperf2`` that is the only place a spoofed trigger shows (``UnexpectedTargetError``);
6. a tool that ran but measured nothing fails the test here, rather than look complete.

Measuring the result is phase 7 (M1-12), and evaluating it phase 8 (M1-13). The pods are
left for cleanup (M1-16), and stop on their own at their deadline.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from dokimi.k8s.deadlines import DeadlineBudget, workload_budget
from dokimi.k8s.ownership import RunOwnership
from dokimi.k8s.resources import KubernetesResources, ResourceClient
from dokimi.model import Duration, TestType
from dokimi.run_identity import RunId
from dokimi.testtypes import plugin_for
from dokimi.testtypes.interface import DeployedWorkload, Placement, Role, TestTypePlugin
from dokimi.testtypes.lifecycle import deploy, start_when_ready, wait_for_completion

if TYPE_CHECKING:
    from dokimi.execution.context import RunContext

__all__ = ["NetworkTestResult", "ToolFailedError", "run_network_test"]

#: Beyond the measurement and its warm-up, how long completion may take to show in the
#: pod's status: the client's exit, the kubelet's report, our next poll.
_COMPLETION_GRACE_SECONDS = 60.0


class ToolFailedError(RuntimeError):
    """The test ran to completion, and its tool reported that it measured nothing."""


@runtime_checkable
class _Completion(Protocol):
    exit_code: int
    summary: str

    @property
    def succeeded(self) -> bool: ...


@runtime_checkable
class _ReportsCompletion(Protocol):
    """A plugin that can say how its test finished, as ``iperf2`` can."""

    def completion(self, deployed: DeployedWorkload) -> _Completion | None: ...


@dataclass(frozen=True, slots=True)
class NetworkTestResult:
    """What one pair's test left behind for the phases after it."""

    test_id: str
    pods: Mapping[Role, str]
    #: The workload's run metadata: images, warm-up, connectivity ([ADR-04-04], [ADR-05-03]).
    run_metadata: Mapping[str, str]
    #: The tool's own summary of its run, if the plugin reports one.
    summary: str


def run_network_test(
    context: RunContext,
    *,
    test_type: str,
    protocol: str,
    source: str,
    target: str,
    bandwidth: str,
    duration: str,
    cluster: ResourceClient | None = None,
    plugin: TestTypePlugin | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> NetworkTestResult:
    """Deploy one pair, pass its readiness gate, start it, and wait for it to finish.

    *cluster* and *plugin* are the real ones unless given, which only tests do.

    Raises:
        ConfigError: on a parameter the test type does not accept.
        ReadinessGateError: a pod never became ``Ready`` (``scheduling``, ``image_pull``).
        CompletionTimeoutError: the test did not finish in time.
        ToolFailedError: it finished, and measured nothing.
        UnexpectedTargetError: for ``iperf2``, it measured another address than the server's.
    """
    kind = TestType.parse(test_type)
    plugin = plugin or plugin_for(kind, image=context.images.get(kind.value))
    params = plugin.parameter_schema().validate(
        {"protocol": protocol, "targetBandwidth": bandwidth, "duration": duration}
    )
    spec = plugin.workload(
        params, Placement(context.namespace, context.run_id, context.test_id, source, target)
    )
    budget = _budget(duration, spec.run_metadata)

    resources = cluster if cluster is not None else KubernetesResources.from_environment()
    # deploy() is the one path that creates a test's pods, bounded and run-owned by
    # construction (ADR-05-02).
    deploy(RunOwnership(resources, RunId(context.run_id)), spec, budget)
    deployed = start_when_ready(plugin, resources, spec, clock=clock, sleep=sleep)
    wait_for_completion(
        plugin,
        deployed,
        timeout=budget.test_duration + budget.warmup + _COMPLETION_GRACE_SECONDS,
        clock=clock,
        sleep=sleep,
    )

    summary = ""
    if isinstance(plugin, _ReportsCompletion):
        completion = plugin.completion(deployed)
        if completion is not None:
            if not completion.succeeded:
                raise ToolFailedError(
                    f"{context.test_id}: {kind.value} exited {completion.exit_code}"
                    + ("" if completion.summary else " without a summary line")
                    + ", so it measured nothing"
                )
            summary = completion.summary
    return NetworkTestResult(
        test_id=context.test_id,
        pods={role: spec.pod_name(role) for role in spec.pods},
        run_metadata=dict(spec.run_metadata),
        summary=summary,
    )


def _budget(duration: str, run_metadata: Mapping[str, str]) -> DeadlineBudget:
    """The pods' deadline, from the duration and the warm-up pass they actually run."""
    warmup = float(run_metadata.get("warmup_duration_seconds", "0") or 0)
    return workload_budget(float(Duration.parse(duration).seconds), warmup_seconds=warmup)
