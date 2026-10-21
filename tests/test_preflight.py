"""The run-level preflight (ADR-05-07): each check, and the name each failure carries.

M1-07's first acceptance criterion is that every failure mode is its own named error,
so each test here asserts the check a failure names, not only that it failed. The
others, that a failure leaves nothing running and that the standalone command shares
this code path, are about ``dokimi execute`` and live in ``test_cli.py``.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from dokimi.cli import commands
from dokimi.formats import parse_file
from dokimi.k8s.deadlines import DEFAULT_PREFLIGHT_SECONDS, DEFAULT_STARTUP_SECONDS
from dokimi.k8s.resources import POD
from dokimi.metrics.prometheus import (
    DEFAULT_QUERY_ACCESS_TIMEOUT_SECONDS,
    PrometheusClient,
    PrometheusUnreachableError,
    Response,
)
from dokimi.model import TestDefinition
from dokimi.planning import NodeInventory, Plan, discover, plan_run
from dokimi.preflight import (
    PROBE_COMPLETION_TIMEOUT_SECONDS,
    Check,
    ClusterNotReadyError,
    PreflightReport,
    check_cluster,
    check_node_count,
    node_exporter_reachability,
)
from dokimi.testtypes.interface import DeployedWorkload, Role
from dokimi.testtypes.iperf2 import Iperf2
from dokimi.testtypes.lifecycle import DEFAULT_READINESS_TIMEOUT_SECONDS
from dokimi.testtypes.probe import ProbeCheck, ProbeResult
from tests.fake_cluster import FakeCluster, node
from tests.fake_probe import FakeProbe, pod_status
from tests.fake_resources import FakeResources

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "worker-01-to-worker-02-udp.yaml"
RUN_ID = "20260101-100000-4f2a9c"
NAMESPACE = "dokimi-iperf2"


def definition() -> TestDefinition:
    return parse_file(EXAMPLE)


def inventory(*nodes: dict[str, object]) -> NodeInventory:
    return discover(FakeCluster(nodes=nodes or (node("worker-01"), node("worker-02"))))


def plan() -> Plan:
    return plan_run(definition(), inventory())


class Clock:
    """Time that moves only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def check(
    probe: FakeProbe,
    resources: FakeResources | None = None,
    nodes: tuple[dict[str, object], ...] = (),
) -> PreflightReport:
    clock = Clock()
    return check_cluster(
        definition(),
        plan(),
        inventory(*nodes),
        resources=resources or FakeResources(),
        run_id=RUN_ID,
        namespace=NAMESPACE,
        cluster="test-cluster",
        images={},
        plugin=probe,
        readiness_timeout=10,
        completion_timeout=10,
        clock=clock,
        sleep=clock.sleep,
    )


def failed_check(probe: FakeProbe, *nodes: dict[str, object]) -> ClusterNotReadyError:
    with pytest.raises(ClusterNotReadyError) as raised:
        check(probe, nodes=nodes)
    return raised.value


# -- what a passed preflight did -------------------------------------------------------


def test_a_ready_cluster_passes_with_the_first_pair_in_probe_mode() -> None:
    resources = FakeResources()
    probe = FakeProbe()

    report = check(probe, resources)

    assert report.nodes == ("worker-01", "worker-02")
    assert report.pair.test_id == "pair-001"
    assert report.pods == {
        Role.SERVER: f"run-{RUN_ID}-pair-001-preflight-server",
        Role.CLIENT: f"run-{RUN_ID}-pair-001-preflight-client",
    }
    assert "arrived" in report.probe
    # Through deploy(): bounded, run-owned, server first; removal is the caller's.
    created = [manifest for kind, manifest in resources.created if kind is POD]
    assert [pod["metadata"]["labels"]["app.kubernetes.io/component"] for pod in created] == [
        "server",
        "client",
    ]
    assert all(pod["spec"]["activeDeadlineSeconds"] > 0 for pod in created)
    assert all(pod["metadata"]["labels"]["dokimi.io/run-id"] == RUN_ID for pod in created)
    assert len(probe.started) == 1


def test_the_probe_runs_on_the_planned_nodes() -> None:
    probe = FakeProbe()

    check(probe)

    (spec,) = probe.specs
    nodes = {
        role: pod["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchFields"][0]["values"]
        for role, pod in spec.pods.items()
    }
    assert nodes == {Role.CLIENT: ["worker-01"], Role.SERVER: ["worker-02"]}


# -- each failure, by name ---------------------------------------------------------------


def test_a_node_gone_since_planning_is_node_count() -> None:
    error = failed_check(FakeProbe(), node("worker-01"), node("worker-03"))

    assert error.check is Check.NODE_COUNT
    assert str(error).startswith("cluster not ready (node_count): ")
    assert "needs 2 node(s) and 1 of them can take a test pod now" in str(error)
    assert "worker-02 no longer exists" in str(error)


def test_a_node_that_no_longer_accepts_pods_is_node_count() -> None:
    probe = FakeProbe()

    error = failed_check(probe, node("worker-01"), node("worker-02", ready="False"))

    assert error.check is Check.NODE_COUNT
    assert "worker-02 no longer accepts a test pod: it is not Ready" in str(error)
    # Nothing is deployed for a plan the cluster can no longer hold.
    assert probe.specs == []


def test_a_pod_that_cannot_be_placed_is_scheduling() -> None:
    statuses = {
        Role.SERVER: pod_status(Role.SERVER, waiting="Unschedulable"),
        Role.CLIENT: pod_status(Role.CLIENT),
    }
    probe = FakeProbe(statuses=statuses)

    error = failed_check(probe)

    assert error.check is Check.SCHEDULING
    assert "the probe of pair-001 (worker-01 -> worker-02)" in str(error)
    assert probe.started == []


def test_an_image_that_cannot_be_pulled_is_image_pull_not_scheduling() -> None:
    # Its own signal (ADR-05-07): a different thing to fix than a node that cannot take
    # the pod, read from the gate's category, never from its message.
    statuses = {role: pod_status(role, waiting="ImagePullBackOff") for role in Role}

    error = failed_check(FakeProbe(statuses=statuses))

    assert error.check is Check.IMAGE_PULL
    assert str(error).startswith("cluster not ready (image_pull): ")


def test_a_client_the_job_cannot_reach_is_connectivity() -> None:
    probe = FakeProbe(start_error=ConnectionError("could not start the client: timed out"))

    error = failed_check(probe)

    assert error.check is Check.CONNECTIVITY
    assert "the Job could not reach the client pod" in str(error)


def test_an_ipv6_only_pod_network_is_connectivity() -> None:
    statuses = {role: pod_status(role) | {"podIPs": [{"ip": "fd00::1"}]} for role in Role}

    error = failed_check(FakeProbe(statuses=statuses))

    assert error.check is Check.CONNECTIVITY
    assert "no IPv4 address" in str(error)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ProbeResult(ProbeCheck.CONNECTIVITY, "no UDP arrived"), Check.CONNECTIVITY),
        (ProbeResult(ProbeCheck.DNS, "kubernetes.default.svc did not resolve"), Check.DNS),
    ],
)
def test_the_probes_own_finding_names_the_check(result: ProbeResult, expected: Check) -> None:
    error = failed_check(FakeProbe(result=result))

    assert error.check is expected
    where = "the probe of pair-001 (worker-01 -> worker-02)"
    assert str(error) == f"cluster not ready ({expected.value}): {where}: {result.detail}"


def test_a_probe_that_never_finishes_is_connectivity() -> None:
    class NeverDone(FakeProbe):
        def probe_result(self, deployed: DeployedWorkload) -> ProbeResult | None:
            return None

    error = failed_check(NeverDone())

    assert error.check is Check.CONNECTIVITY
    assert "did not finish its exchange" in str(error)


def test_every_check_has_its_own_name() -> None:
    names = [check.value for check in Check]

    assert len(set(names)) == len(names)
    assert set(names) == {
        "node_count",
        "scheduling",
        "image_pull",
        "connectivity",
        "dns",
        "prometheus_unreachable",
    }
    # The Prometheus check names itself the same way, from its own module.
    assert Check.PROMETHEUS_UNREACHABLE.value == PrometheusUnreachableError.reason


def test_check_node_count_alone_returns_the_nodes_it_checked() -> None:
    assert check_node_count(plan(), inventory()) == ("worker-01", "worker-02")


# -- the node-exporter: a warning, never a failure -------------------------------------


def targets(*entries: Mapping[str, object], status: int = 200) -> PrometheusClient:
    import json  # noqa: PLC0415

    body = json.dumps({"status": "success", "data": {"activeTargets": list(entries)}}).encode()
    return PrometheusClient("http://prometheus:9090", transport=lambda _url: Response(status, body))


def test_scraped_node_exporters_say_nothing() -> None:
    up = {"health": "up", "labels": {"node": "worker-01"}}
    pending = {"health": "unknown", "labels": {"node": "worker-02"}}

    assert node_exporter_reachability(targets(up, pending), "node-exporter") is None


def test_a_node_exporter_prometheus_cannot_scrape_is_named_with_its_error() -> None:
    down = {"health": "down", "labels": {"node": "worker-02"}, "lastError": "connection refused"}

    warning = node_exporter_reachability(targets(down), "node-exporter")

    assert warning == (
        "Prometheus cannot scrape 1 of 1 node-exporter target(s): worker-02: connection refused"
    )


def test_no_node_exporter_target_at_all_is_said() -> None:
    assert node_exporter_reachability(targets(), "node-exporter") == (
        "Prometheus has no node-exporter target"
    )


def test_unreadable_targets_are_said_not_raised() -> None:
    def refused(_url: str) -> Response:
        raise ConnectionRefusedError("connection refused")

    client = PrometheusClient("http://prometheus:9090", transport=refused)

    warning = node_exporter_reachability(client, "node-exporter")
    assert warning is not None
    assert warning.startswith("could not read Prometheus's targets")


# -- the time it may take ------------------------------------------------------------------


def test_the_jobs_preflight_term_covers_every_wait_in_phase_4() -> None:
    # The Job's deadline is also when its claim expires (ADR-05-13). A preflight that
    # could outlast its term would eat into the time the tests were budgeted.
    trigger = 5 * (5.0 + 1.0)  # tcp_trigger's attempts, each a timeout and a pause
    waits = (
        DEFAULT_QUERY_ACCESS_TIMEOUT_SECONDS
        + DEFAULT_READINESS_TIMEOUT_SECONDS
        + trigger
        + PROBE_COMPLETION_TIMEOUT_SECONDS
        + commands.POD_REMOVAL_TIMEOUT_SECONDS
    )
    assert waits <= DEFAULT_PREFLIGHT_SECONDS
    assert DEFAULT_READINESS_TIMEOUT_SECONDS == DEFAULT_STARTUP_SECONDS


def test_the_real_plugin_is_the_one_probed_when_none_is_given() -> None:
    # Fail closed: the preflight never skips its checks for want of a probe mode.
    resources = FakeResources()
    clock = Clock()
    ticks = itertools.count()

    with pytest.raises(ClusterNotReadyError) as raised:
        check_cluster(
            definition(),
            plan(),
            inventory(),
            resources=resources,
            run_id=RUN_ID,
            namespace=NAMESPACE,
            cluster="test-cluster",
            images={
                "iperf2": Iperf2(image="ghcr.io/dokimi-io/iperf2@sha256:" + "ab" * 32)
                .images()
                .client
            },
            readiness_timeout=0,
            clock=lambda: float(next(ticks)),
            sleep=clock.sleep,
        )

    # The real pods have no status in the fake store, so the gate times out on them.
    assert raised.value.check is Check.SCHEDULING
    created = [manifest for kind, manifest in resources.created if kind is POD]
    client = created[1]["spec"]["containers"][0]
    assert client["args"][-2:] == ["--probe-dns", "kubernetes.default.svc"]


def test_the_real_iperf2_probe_runs_through_the_preflight() -> None:
    # The fake above scripts the probe's result. This one is the real plugin, with the
    # pods' status written as a kubelet would after the client's probe: the path from
    # its report, through completion, to a passed check. Found missing on kind, where
    # completion read the report as a test's summary.
    resources = FakeResources()
    sent: list[tuple[str, int, bytes]] = []
    plugin = Iperf2(
        image="ghcr.io/dokimi-io/iperf2@sha256:" + "ab" * 32,
        trigger=lambda *call: sent.append(call),
    )
    report = "server=10.244.2.20\nconnectivity=ok\ndns=ok\n"

    def kubelet(kind: object, _namespace: str, body: dict[str, Any]) -> None:
        if kind is not POD:
            return
        role = Role(body["metadata"]["labels"]["app.kubernetes.io/component"])
        body["status"] = pod_status(role)
        if role is Role.CLIENT:
            terminated = {"exitCode": 0, "reason": "Completed", "message": report}
            body["status"]["containerStatuses"] = [
                {"name": "iperf2", "state": {"terminated": terminated}}
            ]

    resources.before_create = kubelet
    clock = Clock()

    result = check_cluster(
        definition(),
        plan(),
        inventory(),
        resources=resources,
        run_id=RUN_ID,
        namespace=NAMESPACE,
        cluster="test-cluster",
        images={"iperf2": plugin.images().client},
        plugin=plugin,
        clock=clock,
        sleep=clock.sleep,
    )

    assert sent == [("10.244.1.10", 5101, b"10.244.2.20\n")]
    assert result.probe == (
        "UDP to the server pod at 10.244.2.20:5001 arrived, and kubernetes.default.svc resolved"
    )
