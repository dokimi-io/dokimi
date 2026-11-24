"""Run-level cluster checks, before any test workload is deployed.

Phase 4, inside the Job, after planning and before suite rendering. Blocking, and in
this order, after the run-exclusivity claim and the Prometheus query check that open the
phase:

1. **node count**: every node the planned matrix needs is still there and still accepts
   a test pod. Phase 3 checked the same nodes against the same facts, so this catches a
   node lost since; a node missing at planning is a ``config_error`` there, not a
   preflight failure here.
2. **scheduling** and **image pull**: the first planned pair is deployed with its own
   spec, on its own nodes and from its own image, and must pass the per-test readiness
   gate. :class:`~dokimi.testtypes.lifecycle.ReadinessGateError` keeps the two apart, as
   it does for a test.
3. **connectivity** and **DNS**: the pair's client runs in its probe mode
   (:mod:`dokimi.testtypes.probe`). It exchanges one second of traffic with the server
   and resolves a cluster name, the two checks only a pod can make.

A failed check is a :class:`ClusterNotReadyError` that names it: ``cluster not ready
(<check>)``, the same form the Prometheus check already reports
(:class:`~dokimi.metrics.prometheus.PrometheusUnreachableError`), and exit ``4``.
Nothing in the matrix runs.

The probe's pods are this module's to create and the caller's to remove: ``dokimi
execute`` removes every test pod of the run before it renders the suite, and again on
every way out, so a failed check leaves nothing running. The standalone
``dokimi preflight`` is the same function, called by the same ``execute``, which stops
after this phase.

A preflight that passes is a floor, not a guarantee: one pair under no load says nothing
about the full matrix's concurrency.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from dokimi.k8s.deadlines import workload_budget
from dokimi.k8s.ownership import RunOwnership
from dokimi.k8s.resources import ResourceClient
from dokimi.metrics.prometheus import PrometheusClient, PrometheusError, PrometheusUnreachableError
from dokimi.model import TestDefinition
from dokimi.planning import NodeInventory, Plan, PlannedPair
from dokimi.run_identity import RunId
from dokimi.testtypes import parameters_for, plugin_for
from dokimi.testtypes.interface import (
    DeployedWorkload,
    NoIPv4AddressError,
    Placement,
    Role,
    WorkloadImage,
)
from dokimi.testtypes.lifecycle import (
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    CompletionTimeoutError,
    ReadinessGateError,
    WorkloadLostError,
    deploy,
    start_when_ready,
    wait_for_completion,
)
from dokimi.testtypes.probe import PROBE_SECONDS, ProbeCheck, Probes

__all__ = [
    "PROBE_COMPLETION_TIMEOUT_SECONDS",
    "PROBE_TEST_ID_SUFFIX",
    "Check",
    "ClusterNotReadyError",
    "PreflightReport",
    "check_cluster",
    "check_node_count",
    "node_exporter_reachability",
    "probe_first_pair",
]

#: How long the probe may take from ``start()`` to the client's exit: its exchange, the
#: client's own bound on a connection that never answers (15 s), a name lookup bounded
#: the same way, and the kubelet reporting the exit.
PROBE_COMPLETION_TIMEOUT_SECONDS = 60.0

#: Appended to the first pair's ``test_id`` for the probe's pods, so that nothing reads
#: them, in a log or a label, as the test itself.
PROBE_TEST_ID_SUFFIX = "-preflight"


class Check(enum.Enum):
    """The run-level preflight's checks, as a failure names them.

    One run-level category, ``cluster not ready``, and these as its reason, the way a
    refused claim is the reason ``run_already_active``. ``scheduling`` and
    ``image_pull`` are spelled as the per-test categories are, because the
    preflight finds them with the per-test gate. They stay run-level here: the run has
    no test yet.
    """

    NODE_COUNT = "node_count"
    SCHEDULING = "scheduling"
    IMAGE_PULL = "image_pull"
    CONNECTIVITY = ProbeCheck.CONNECTIVITY.value
    DNS = ProbeCheck.DNS.value
    PROMETHEUS_UNREACHABLE = PrometheusUnreachableError.reason


class ClusterNotReadyError(RuntimeError):
    """A run-level preflight check failed: the run stops, and exits ``4``."""

    def __init__(self, check: Check, detail: str) -> None:
        self.check = check
        self.reason = check.value
        self.detail = detail
        super().__init__(f"cluster not ready ({check.value}): {detail}")


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """What a passed preflight established, for the Job's log."""

    #: The nodes the plan needs, each found and selectable.
    nodes: tuple[str, ...]
    #: The pair the probe reused, and its pods by role.
    pair: PlannedPair
    pods: Mapping[Role, str]
    #: The probe's own account of its exchange and lookup.
    probe: str


def check_cluster(
    definition: TestDefinition,
    plan: Plan,
    inventory: NodeInventory,
    *,
    resources: ResourceClient,
    run_id: str,
    namespace: str,
    cluster: str,
    images: Mapping[str, WorkloadImage],
    plugin: Probes | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    completion_timeout: float = PROBE_COMPLETION_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PreflightReport:
    """The cluster checks, in order. Raises :class:`ClusterNotReadyError` on the first failure.

    *inventory* is the cluster's nodes read now, not phase 3's. *cluster* is the name the
    run was submitted with, which the probe's pods carry like a test's. *plugin* is the real
    one unless given, which only tests do.
    """
    nodes = check_node_count(plan, inventory)
    pair, pods, probe = probe_first_pair(
        definition,
        plan,
        resources=resources,
        run_id=run_id,
        namespace=namespace,
        cluster=cluster,
        images=images,
        plugin=plugin,
        readiness_timeout=readiness_timeout,
        completion_timeout=completion_timeout,
        clock=clock,
        sleep=sleep,
    )
    return PreflightReport(nodes=nodes, pair=pair, pods=pods, probe=probe)


def check_node_count(plan: Plan, inventory: NodeInventory) -> tuple[str, ...]:
    """Every node the plan needs, still present and selectable. Returns their names."""
    needed = sorted({node.name for pair in plan.pairs for node in (pair.source, pair.target)})
    problems = []
    for name in needed:
        node = inventory.get(name)
        if node is None:
            problems.append(f"{name} no longer exists")
        elif not node.schedulable:
            problems.append(
                f"{name} no longer accepts a test pod: " + "; ".join(node.unschedulable_reasons)
            )
    if problems:
        available = len(needed) - len(problems)
        raise ClusterNotReadyError(
            Check.NODE_COUNT,
            f"the planned matrix needs {len(needed)} node(s) and {available} of them can take "
            f"a test pod now. Since planning, " + "; ".join(problems),
        )
    return tuple(needed)


def probe_first_pair(
    definition: TestDefinition,
    plan: Plan,
    *,
    resources: ResourceClient,
    run_id: str,
    namespace: str,
    cluster: str,
    images: Mapping[str, WorkloadImage],
    plugin: Probes | None = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    completion_timeout: float = PROBE_COMPLETION_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[PlannedPair, dict[Role, str], str]:
    """Deploy the first planned pair in probe mode, gate it, start it, and read its report.

    Returns the pair, its probe pods by role, and the probe's own account. Every way the
    pair can fail is a :class:`ClusterNotReadyError` naming the check.
    """
    pair = plan.pairs[0]
    kind = definition.test_type
    if plugin is None:
        image = images.get(kind.value)
        if image is None:
            # submit resolves every image; the Job resolving one here would run
            # an image nobody chose. A bug, not a finding about the cluster.
            raise RuntimeError(f"this run has no image for {kind.value}")
        candidate = plugin_for(kind, image=image)
        if not isinstance(candidate, Probes):
            # Fail closed: a run whose cluster was never checked must not look as if it was.
            raise TypeError(f"the {kind.value} plugin has no probe mode for the preflight")
        plugin = candidate
    params = plugin.parameter_schema().validate(parameters_for(definition))
    placement = Placement(
        namespace,
        run_id,
        pair.test_id + PROBE_TEST_ID_SUFFIX,
        pair.source.name,
        pair.target.name,
        cluster,
    )
    spec = plugin.probe(params, placement)
    where = f"the probe of {pair.test_id} ({pair.source.name} -> {pair.target.name})"

    deploy(
        RunOwnership(resources, RunId(run_id)),
        spec,
        workload_budget(PROBE_SECONDS, warmup_seconds=0),
    )
    try:
        deployed = start_when_ready(
            plugin, resources, spec, timeout=readiness_timeout, clock=clock, sleep=sleep
        )
    except ReadinessGateError as error:
        raise ClusterNotReadyError(Check(error.category.value), f"{where}: {error}") from error
    except NoIPv4AddressError as error:
        raise ClusterNotReadyError(Check.CONNECTIVITY, f"{where}: {error}") from error
    except OSError as error:
        # start() could not reach the client: the Job's own path into the pod network.
        raise ClusterNotReadyError(
            Check.CONNECTIVITY, f"{where}: the Job could not reach the client pod: {error}"
        ) from error

    try:
        wait_for_completion(
            _ProbeEnded(plugin), deployed, timeout=completion_timeout, clock=clock, sleep=sleep
        )
        result = plugin.probe_result(deployed)
    except CompletionTimeoutError as error:
        raise ClusterNotReadyError(
            Check.CONNECTIVITY, f"{where}: the client did not finish its exchange: {error}"
        ) from error
    except WorkloadLostError as error:
        raise ClusterNotReadyError(Check.SCHEDULING, f"{where}: {error}") from error
    if result is None:
        raise ClusterNotReadyError(Check.CONNECTIVITY, f"{where}: the client reported nothing")
    if result.failed is not None:
        raise ClusterNotReadyError(Check(result.failed.value), f"{where}: {result.detail}")
    return pair, {role: spec.pod_name(role) for role in spec.pods}, result.detail


class _ProbeEnded:
    """Completion, as the probe reports it.

    Not the plugin's ``is_complete()``: that reads how a *test* ends, and a probe's
    report is not a test's summary.
    """

    def __init__(self, plugin: Probes) -> None:
        self._plugin = plugin

    def is_complete(self, deployed: DeployedWorkload) -> bool:
        return self._plugin.probe_result(deployed) is not None


def node_exporter_reachability(client: PrometheusClient, job: str) -> str | None:
    """Why Prometheus does not scrape every node-exporter target, or ``None`` if it does.

    A warning, never a failure: node metrics explain a result, they never decide one.
    A target not scraped yet is not reported; the check runs once, late in
    phase 4, and a first scrape still pending then is no finding.
    """
    try:
        targets = client.targets(job)
    except (OSError, PrometheusError) as error:
        return f"could not read Prometheus's targets: {error}"
    if not targets:
        return "Prometheus has no node-exporter target"
    down = [target for target in targets if target.health == "down"]
    if not down:
        return None
    return (
        f"Prometheus cannot scrape {len(down)} of {len(targets)} node-exporter target(s): "
        + "; ".join(
            f"{target.labels.get('node') or target.url}: {target.last_error or 'down'}"
            for target in down
        )
    )
