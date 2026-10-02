"""Deployment, the per-test readiness gate, and completion, for any test type.

The acceptance criterion that matters most here: ``start()`` is never called when a pod
has not reached ``Ready`` (ADR-05-07). The plugin below records every call, so "never
called" is an assertion about what happened rather than about what the code intends.
"""

from __future__ import annotations

from typing import Any

import pytest

from dokimi.k8s.deadlines import DEFAULT_STARTUP_SECONDS, DeadlineBudget
from dokimi.k8s.ownership import (
    COMPONENT_LABEL,
    RUN_ID_LABEL,
    TEST_ID_LABEL,
    RunOwnership,
)
from dokimi.k8s.resources import POD, Manifest
from dokimi.run_identity import RunId
from dokimi.testtypes.interface import (
    DeployedWorkload,
    NoIPv4AddressError,
    Placement,
    Role,
    WorkloadSpec,
)
from dokimi.testtypes.iperf2 import Iperf2
from dokimi.testtypes.lifecycle import (
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    CompletionTimeoutError,
    ErrorCategory,
    ReadinessGateError,
    await_ready,
    deploy,
    pod_is_ready,
    start_when_ready,
    wait_for_completion,
)
from tests.fake_resources import FakeResources

RUN = RunId("20260924-143012-k3f9x2")
NAMESPACE = "dokimi-iperf2"
PLACEMENT = Placement(NAMESPACE, str(RUN), "pair-001", "worker-01", "worker-02")
BUDGET = DeadlineBudget(
    test_duration=30, startup=300, warmup=10, direct_read=30, scrape_confirmation=120
)


class Recorder:
    """A test type that records start() and answers is_complete() from a script."""

    name = "recorder"

    def __init__(self, completes_after: int = 0) -> None:
        self.started: list[DeployedWorkload] = []
        self.polls = 0
        self.completes_after = completes_after

    def start(self, deployed: DeployedWorkload) -> None:
        self.started.append(deployed)

    def is_complete(self, deployed: DeployedWorkload) -> bool:
        self.polls += 1
        return self.polls > self.completes_after


class Clock:
    """Time that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def cluster() -> FakeResources:
    return FakeResources()


@pytest.fixture
def spec() -> WorkloadSpec:
    plugin = Iperf2(image="ghcr.io/dokimi-io/iperf2@sha256:" + "c" * 64)
    params = plugin.parameter_schema().validate(
        {"protocol": "udp", "targetBandwidth": "1G", "duration": "30s"}
    )
    return plugin.workload(params, PLACEMENT)


def set_status(cluster: FakeResources, spec: WorkloadSpec, role: Role, status: Manifest) -> None:
    cluster.objects[("Pod", NAMESPACE, spec.pod_name(role))]["status"] = status


def running(ip: str) -> Manifest:
    return {"phase": "Running", "podIP": ip, "conditions": [{"type": "Ready", "status": "True"}]}


def unschedulable() -> Manifest:
    return {
        "phase": "Pending",
        "conditions": [
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/4 nodes are available: 1 node(s) had untolerated taint",
            }
        ],
    }


def deployed_to(cluster: FakeResources, spec: WorkloadSpec) -> None:
    deploy(RunOwnership(cluster, RUN), spec, BUDGET)


def gate(cluster: FakeResources, spec: WorkloadSpec, **kwargs: Any) -> DeployedWorkload:
    clock = Clock()
    return await_ready(cluster, spec, timeout=10, clock=clock, sleep=clock.sleep, **kwargs)


# -- deploy ------------------------------------------------------------------------------


def test_deploy_creates_the_server_before_the_client(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    deployed_to(cluster, spec)

    pods = [manifest["metadata"]["name"] for kind, manifest in cluster.created if kind is POD]
    assert pods == [spec.pod_name(Role.SERVER), spec.pod_name(Role.CLIENT)]


def test_deployed_pods_are_bounded_and_run_owned(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    # ADR-05-02: labels make cleanup possible, deadlines make it unnecessary.
    deployed_to(cluster, spec)

    for role in Role:
        pod = cluster.get(POD, NAMESPACE, spec.pod_name(role))
        labels = pod["metadata"]["labels"]
        assert pod["spec"]["activeDeadlineSeconds"] == BUDGET.active_deadline_seconds
        assert labels[RUN_ID_LABEL] == str(RUN)
        assert labels[TEST_ID_LABEL] == "pair-001"
        assert labels[COMPONENT_LABEL] == role.value
        assert pod["metadata"]["ownerReferences"]


# -- the readiness gate ------------------------------------------------------------------


def test_the_gate_passes_when_both_pods_are_ready(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, running("10.0.2.7"))
    set_status(cluster, spec, Role.CLIENT, running("10.0.1.5"))

    deployed = gate(cluster, spec)

    assert deployed.pod_ip(Role.SERVER) == "10.0.2.7"
    assert deployed.pod_ip(Role.CLIENT) == "10.0.1.5"


def dual_stack(*ips: str) -> Manifest:
    return running(ips[0]) | {"podIPs": [{"ip": ip} for ip in ips]}


def test_a_dual_stack_pod_is_addressed_over_ipv4_even_when_ipv6_is_primary(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    # ADR-05-03: IPv4 when both families are available. On an IPv6-primary cluster
    # status.podIP is the IPv6 address, and the client script refuses it.
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, dual_stack("fd00:10:244::7", "10.0.2.7"))
    set_status(cluster, spec, Role.CLIENT, dual_stack("fd00:10:244::5", "10.0.1.5"))

    deployed = gate(cluster, spec)

    assert deployed.pod_ip(Role.SERVER) == "10.0.2.7"
    assert deployed.pod_ip(Role.CLIENT) == "10.0.1.5"


def test_an_ipv6_only_pod_is_refused_by_name(cluster: FakeResources, spec: WorkloadSpec) -> None:
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, dual_stack("fd00:10:244::7"))
    set_status(cluster, spec, Role.CLIENT, running("10.0.1.5"))

    deployed = gate(cluster, spec)

    with pytest.raises(NoIPv4AddressError, match=r"server pod .* only fd00:10:244::7"):
        deployed.pod_ip(Role.SERVER)


def test_a_pod_that_never_becomes_ready_is_a_scheduling_error_naming_why(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, running("10.0.2.7"))
    set_status(cluster, spec, Role.CLIENT, unschedulable())

    with pytest.raises(ReadinessGateError, match="untolerated taint") as raised:
        gate(cluster, spec)

    assert raised.value.test_id == "pair-001"
    assert set(raised.value.reasons) == {Role.CLIENT}
    assert raised.value.category is ErrorCategory.SCHEDULING


def test_the_gate_waits_its_whole_timeout_and_no_longer(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    deployed_to(cluster, spec)
    clock = Clock()

    with pytest.raises(ReadinessGateError):
        await_ready(cluster, spec, timeout=10, poll_interval=2, clock=clock, sleep=clock.sleep)

    assert clock.now == 10


def test_a_pod_that_becomes_ready_later_passes(cluster: FakeResources, spec: WorkloadSpec) -> None:
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, running("10.0.2.7"))
    clock = Clock()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        set_status(cluster, spec, Role.CLIENT, running("10.0.1.5"))

    deployed = await_ready(cluster, spec, timeout=10, clock=clock, sleep=sleep)

    assert pod_is_ready(deployed.pods[Role.CLIENT])
    assert clock.slept == [2.0]


def pulling_failed(reason: str) -> Manifest:
    return {
        "phase": "Pending",
        "containerStatuses": [{"name": "iperf2", "state": {"waiting": {"reason": reason}}}],
    }


@pytest.mark.parametrize(
    "reason", ["ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"]
)
def test_an_image_pull_failure_is_its_own_category(
    cluster: FakeResources, spec: WorkloadSpec, reason: str
) -> None:
    # "Can't be placed" and "can't fetch its image" point at different fixes, and
    # ADR-05-05 gives the second its own category. ADR-05-07's preflight reuses this gate
    # and must tell them apart from the category, not from the text.
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, running("10.0.2.7"))
    set_status(cluster, spec, Role.CLIENT, pulling_failed(reason))

    with pytest.raises(ReadinessGateError, match=reason) as raised:
        gate(cluster, spec)

    assert raised.value.category is ErrorCategory.IMAGE_PULL
    assert raised.value.categories == {Role.CLIENT: ErrorCategory.IMAGE_PULL}
    assert "could not pull its image" in str(raised.value)


def test_a_pull_failure_outweighs_an_unschedulable_pod(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    # The unschedulable pod has not tried to pull yet; the pull failure is certain.
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, unschedulable())
    set_status(cluster, spec, Role.CLIENT, pulling_failed("ErrImagePull"))

    with pytest.raises(ReadinessGateError) as raised:
        gate(cluster, spec)

    assert raised.value.categories == {
        Role.SERVER: ErrorCategory.SCHEDULING,
        Role.CLIENT: ErrorCategory.IMAGE_PULL,
    }
    assert raised.value.category is ErrorCategory.IMAGE_PULL


def test_a_pod_that_ended_before_it_was_ready_fails_the_gate_at_once(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, {"phase": "Failed"})
    clock = Clock()

    with pytest.raises(ReadinessGateError, match="ended"):
        await_ready(cluster, spec, timeout=10, clock=clock, sleep=clock.sleep)

    assert clock.slept == []  # it can never become Ready, so there is nothing to wait for


def test_a_pod_that_was_deleted_fails_the_gate(cluster: FakeResources, spec: WorkloadSpec) -> None:
    with pytest.raises(ReadinessGateError, match="no longer exists"):
        gate(cluster, spec)


def test_a_running_pod_is_not_ready_until_its_condition_says_so() -> None:
    assert not pod_is_ready({"status": {"phase": "Running", "conditions": []}})
    assert not pod_is_ready(
        {"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]}}
    )
    assert pod_is_ready({"status": running("10.0.0.1")})


def test_the_gate_fits_inside_the_workload_deadline() -> None:
    # The startup term budgets scheduling, image pull and this gate together; a longer
    # gate would wait on pods the cluster is about to kill (ADR-05-13).
    assert DEFAULT_READINESS_TIMEOUT_SECONDS <= DEFAULT_STARTUP_SECONDS


# -- start is never called before the gate -----------------------------------------------


def test_start_is_never_called_when_a_pod_is_not_ready(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    # The acceptance criterion (ADR-05-07): a gate failure is a scheduling error and
    # start() is never reached.
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, running("10.0.2.7"))
    set_status(cluster, spec, Role.CLIENT, unschedulable())
    plugin = Recorder()
    clock = Clock()

    with pytest.raises(ReadinessGateError):
        start_when_ready(plugin, cluster, spec, timeout=10, clock=clock, sleep=clock.sleep)

    assert plugin.started == []


def test_start_is_called_once_with_what_the_gate_saw(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    deployed_to(cluster, spec)
    set_status(cluster, spec, Role.SERVER, running("10.0.2.7"))
    set_status(cluster, spec, Role.CLIENT, running("10.0.1.5"))
    plugin = Recorder()

    deployed = start_when_ready(plugin, cluster, spec, timeout=10)

    assert plugin.started == [deployed]
    assert all(pod_is_ready(pod) for pod in deployed.pods.values())


# -- completion --------------------------------------------------------------------------


def _deployed(cluster: FakeResources, spec: WorkloadSpec) -> DeployedWorkload:
    return DeployedWorkload(spec=spec, pods={}, cluster=cluster)


def test_completion_is_polled_until_it_is_reported(
    cluster: FakeResources, spec: WorkloadSpec
) -> None:
    plugin = Recorder(completes_after=3)
    clock = Clock()

    wait_for_completion(
        plugin,
        _deployed(cluster, spec),
        timeout=60,
        clock=clock,
        sleep=clock.sleep,
    )

    assert plugin.polls == 4


def test_a_test_that_never_completes_times_out(cluster: FakeResources, spec: WorkloadSpec) -> None:
    plugin = Recorder(completes_after=1000)
    clock = Clock()

    with pytest.raises(CompletionTimeoutError, match="pair-001"):
        wait_for_completion(
            plugin,
            _deployed(cluster, spec),
            timeout=10,
            clock=clock,
            sleep=clock.sleep,
        )
