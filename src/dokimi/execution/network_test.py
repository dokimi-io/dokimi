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
6. a tool that ran but measured nothing fails the test here, rather than look complete;
7. the measurement, read directly from both pods' exporters ([ADR-03-01], phase 7): the
   receiver's view is the result, and a result not served is an error, never a pass.

Evaluating it is phase 8, :mod:`dokimi.validation`. The pods stay until the run ends:
``dokimi execute`` removes them before it frees the cluster.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from dokimi.error_categories import ErrorCategory
from dokimi.k8s.deadlines import DEFAULT_DIRECT_READ_SECONDS, workload_budget
from dokimi.k8s.ownership import RunOwnership
from dokimi.k8s.resources import KubernetesResources, ResourceClient
from dokimi.measurement import Measurement, fetch_metrics, measure
from dokimi.model import ConfigError, Duration, TestType
from dokimi.run_identity import RunId
from dokimi.testtypes import plugin_for
from dokimi.testtypes.interface import DeployedWorkload, Placement, Role, TestTypePlugin
from dokimi.testtypes.lifecycle import deploy, start_when_ready, wait_for_completion

if TYPE_CHECKING:
    from dokimi.execution.context import RunContext

__all__ = ["NetworkTestResult", "PairMismatchError", "ToolFailedError", "run_network_test"]

#: Beyond the measurement and its warm-up, how long completion may take to show in the
#: pod's status: the client's exit, the kubelet's report, our next poll.
_COMPLETION_GRACE_SECONDS = 60.0


class ToolFailedError(RuntimeError):
    """The test ran to completion, and its tool reported that it measured nothing."""

    category = ErrorCategory.TOOL_EXECUTION


class PairMismatchError(RuntimeError):
    """The suite names other nodes than the pair this Robot process was started for."""


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
    #: The result, read directly from the exporters: what the verdict is evaluated on.
    measurement: Measurement


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
    read_metrics: Callable[[str], str] = fetch_metrics,
) -> NetworkTestResult:
    """Deploy one pair, pass its readiness gate, start it, wait for it, and read its result.

    *cluster*, *plugin* and *read_metrics* are the real ones unless given, which only
    tests do.

    Raises:
        ConfigError: on a parameter the test type does not accept.
        ReadinessGateError: a pod never became ``Ready`` (``scheduling``, ``image_pull``).
        CompletionTimeoutError: the test did not finish in time.
        ToolFailedError: it finished, and measured nothing.
        UnexpectedTargetError: for ``iperf2``, it measured another address than the server's.
        MetricsMissingError: its exporters served no completed result (``metrics_missing``).
        ExporterParseError: they served one dokimi cannot read (``exporter_parse``).
    """
    if (source, target) != (context.source_node, context.target_node):
        raise PairMismatchError(
            f"{context.test_id}: the suite asks for {source} -> {target}, but this process "
            f"runs the planned pair {context.source_node} -> {context.target_node}"
        )
    kind = TestType.parse(test_type)
    if plugin is None:
        # submit resolves every image and the Job passes it on (ADR-04-08). Resolving a
        # default here instead would run an image nobody chose or saw.
        image = context.images.get(kind.value)
        if image is None:
            raise ConfigError(
                f"this run has no image for {kind.value}; submit resolves it, so submit the "
                "run again with this version of dokimi"
            )
        plugin = plugin_for(kind, image=image)
    if not isinstance(plugin, _ReportsCompletion):
        # Fail closed: without completion() nothing can tell a test that measured from one
        # that measured nothing. ADR-02-01's interface gains it before a second test type.
        raise TypeError(f"the {kind.value} plugin cannot report how its test finished")
    params = plugin.parameter_schema().validate(
        {"protocol": protocol, "targetBandwidth": bandwidth, "duration": duration}
    )
    spec = plugin.workload(
        params,
        Placement(
            context.namespace, context.run_id, context.test_id, source, target, context.cluster
        ),
    )
    budget = workload_budget(
        float(Duration.parse(duration).seconds), warmup_seconds=spec.warmup_seconds
    )

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

    completion = plugin.completion(deployed)
    if completion is None or not completion.succeeded:
        code = "no exit" if completion is None else f"exited {completion.exit_code}"
        raise ToolFailedError(
            f"{context.test_id}: {kind.value} {code}"
            + ("" if completion is not None and completion.summary else " without a summary line")
            + ", so it measured nothing"
        )
    # Phase 7: the same /metrics Prometheus scrapes, read directly, from both pods.
    exporter = plugin.exporter()
    measurement = measure(
        [
            f"http://{deployed.pod_ip(role)}:{exporter.port}{exporter.path}"
            for role in (Role.SERVER, Role.CLIENT)
        ],
        run_id=context.run_id,
        test_id=context.test_id,
        protocol=params.written["protocol"],
        read=read_metrics,
        timeout=DEFAULT_DIRECT_READ_SECONDS,
        clock=clock,
        sleep=sleep,
    )
    return NetworkTestResult(
        test_id=context.test_id,
        pods={role: spec.pod_name(role) for role in spec.pods},
        run_metadata=dict(spec.run_metadata),
        summary=completion.summary,
        measurement=measurement,
    )
