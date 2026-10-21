"""Phase 10's post-run snapshot (M1-15, ADR-05-07): what it records, and what is an anomaly."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from dokimi.diagnostics import (
    SCHEMA_VERSION,
    SNAPSHOT_TIMEOUT_SECONDS,
    AnomalyKind,
    Diagnostics,
    capture,
    write_report,
)
from dokimi.execution import RunArtifacts
from dokimi.k8s.deadlines import DEFAULT_DIAGNOSTICS_SECONDS
from dokimi.k8s.identity import COMPONENT_LABEL, MANAGED_BY, MANAGED_BY_LABEL, RUN_ID_LABEL
from dokimi.k8s.ownership import TEST_ID_LABEL
from dokimi.k8s.resources import POD, Kind, Manifest
from dokimi.planning import Plan, PlannedPair
from dokimi.planning.nodes import node_facts
from dokimi.run_identity import RunId
from tests.fake_cluster import node
from tests.fake_resources import FakeResources

RUN = RunId("20261013-120000-d1agno")
NAMESPACE = "dokimi-iperf2"
NODES = [node("worker-01"), node("worker-02"), node("worker-03")]
PAIR = PlannedPair("pair-001", node_facts(node("worker-01")), node_facts(node("worker-02")))
PLAN = Plan(waves=((PAIR,),))


def pod(
    role: str,
    *,
    node_name: str | None = None,
    phase: str = "Running",
    ready: bool = True,
    reason: str | None = None,
    containers: list[dict[str, Any]] | None = None,
    component: str | None = None,
    test_id: str = "pair-001",
) -> Manifest:
    status: dict[str, Any] = {
        "phase": phase,
        "podIP": "10.244.1.5" if role == "server" else "10.244.3.4",
        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        "containerStatuses": containers
        if containers is not None
        else [{"name": "iperf2", "state": {"running": {}}, "restartCount": 0}],
    }
    if reason is not None:
        status["reason"] = reason
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"run-{RUN}-{test_id}-{role}",
            "namespace": NAMESPACE,
            "labels": {
                MANAGED_BY_LABEL: MANAGED_BY,
                RUN_ID_LABEL: str(RUN),
                TEST_ID_LABEL: test_id,
                COMPONENT_LABEL: component or role,
            },
        },
        "spec": {"nodeName": node_name or ("worker-01" if role == "client" else "worker-02")},
        "status": status,
    }


def terminated(name: str, *, exit_code: int = 0, reason: str = "Completed") -> dict[str, Any]:
    return {
        "name": name,
        "state": {"terminated": {"exitCode": exit_code, "reason": reason}},
        "restartCount": 0,
    }


def cluster_with(*pods: Manifest) -> FakeResources:
    store = FakeResources()
    for manifest in pods:
        store.put(POD, NAMESPACE, manifest)
    return store


def snapshot(
    store: FakeResources,
    tmp_path: Path,
    *,
    started: set[str] | None = None,
    nodes: list[Manifest] | None = None,
    **kwargs: Any,
) -> Diagnostics:
    return capture(
        store,
        run_id=RUN,
        namespace=NAMESPACE,
        plan=PLAN,
        started={"pair-001"} if started is None else started,
        artifacts=artifacts_in(tmp_path),
        list_nodes=lambda: NODES if nodes is None else nodes,
        **kwargs,
    )


def artifacts_in(directory: Path) -> RunArtifacts:
    """The run's artifacts with *directory* as their directory."""
    return RunArtifacts(directory.parent, directory.name)


def kinds(diagnostics: Diagnostics) -> list[AnomalyKind]:
    return [anomaly.kind for anomaly in diagnostics.anomalies]


# -- a clean run --------------------------------------------------------------------------


def test_a_clean_run_records_everything_and_retains_nothing(tmp_path: Path) -> None:
    # Acceptance criterion: captured on passing runs too, as a record worth keeping.
    store = cluster_with(
        pod("client", phase="Succeeded", ready=False, containers=[terminated("iperf2")]),
        pod("server"),
    )
    (tmp_path / "pairs").mkdir()
    (tmp_path / "pairs" / "pair-001.xml").write_text("<robot/>")

    diagnostics = snapshot(store, tmp_path)

    assert diagnostics.complete
    assert diagnostics.anomalies == []
    assert diagnostics.retain is False
    assert diagnostics.namespace_reachable is True
    assert [(p.name.rsplit("-", 1)[1], p.node, p.phase) for p in diagnostics.pods] == [
        ("client", "worker-01", "Succeeded"),
        ("server", "worker-02", "Running"),
    ]
    (pair,) = diagnostics.pairs
    assert pair.down is False
    assert pair.findings == "pairs/pair-001.xml"
    assert pair.pods == {
        "client": f"run-{RUN}-pair-001-client",
        "server": f"run-{RUN}-pair-001-server",
    }


def test_a_finished_client_kept_running_by_its_exporter_is_normal(tmp_path: Path) -> None:
    client = pod(
        "client",
        ready=False,
        containers=[terminated("iperf2"), {"name": "exporter", "state": {"running": {}}}],
    )
    assert snapshot(cluster_with(client, pod("server")), tmp_path).anomalies == []


def test_a_tool_that_exited_non_zero_is_the_tests_result_not_an_anomaly(tmp_path: Path) -> None:
    # The test reports it (ToolFailedError); nothing was missed that phase 10 must catch.
    client = pod(
        "client",
        phase="Failed",
        ready=False,
        containers=[terminated("iperf2", exit_code=69, reason="Error")],
    )
    diagnostics = snapshot(cluster_with(client, pod("server")), tmp_path)

    assert diagnostics.anomalies == []
    # The most common down connection: a client that couldn't reach its server.
    assert diagnostics.pairs[0].down is True
    assert diagnostics.summary()[0].endswith("down: pair-001")


# -- anomalies ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "kind"),
    [("Evicted", AnomalyKind.POD_EVICTED), ("DeadlineExceeded", AnomalyKind.POD_DEADLINE_EXCEEDED)],
)
def test_a_pod_the_cluster_ended_is_an_anomaly(
    tmp_path: Path, reason: str, kind: AnomalyKind
) -> None:
    store = cluster_with(pod("client"), pod("server", phase="Failed", reason=reason))

    diagnostics = snapshot(store, tmp_path)

    assert kinds(diagnostics) == [kind]
    assert diagnostics.retain is True
    assert diagnostics.pairs[0].down is True


@pytest.mark.parametrize(
    ("container", "kind"),
    [
        (
            {"name": "exporter", "state": {"running": {}}, "restartCount": 2},
            AnomalyKind.CONTAINER_RESTARTED,
        ),
        (
            {"name": "exporter", "state": {"waiting": {"reason": "CrashLoopBackOff"}}},
            AnomalyKind.CONTAINER_CRASH_LOOPING,
        ),
        (terminated("iperf2", exit_code=137, reason="OOMKilled"), AnomalyKind.CONTAINER_OOM_KILLED),
    ],
)
def test_an_unhealthy_container_is_an_anomaly(
    tmp_path: Path, container: dict[str, Any], kind: AnomalyKind
) -> None:
    diagnostics = snapshot(
        cluster_with(pod("client"), pod("server", containers=[container])), tmp_path
    )

    assert kind in kinds(diagnostics)
    assert diagnostics.anomalies[0].subject == f"run-{RUN}-pair-001-server/{container['name']}"


def test_a_started_pairs_missing_pod_is_an_anomaly(tmp_path: Path) -> None:
    diagnostics = snapshot(cluster_with(pod("client")), tmp_path)

    assert kinds(diagnostics) == [AnomalyKind.POD_MISSING]
    assert diagnostics.anomalies[0].subject == "pair-001/server"
    assert diagnostics.pairs[0].pods["server"] is None
    assert diagnostics.pairs[0].down is True


def test_a_pair_that_never_started_is_expected_to_have_no_pods(tmp_path: Path) -> None:
    # A run that failed before phase 6: its absence is explained, not an anomaly.
    diagnostics = snapshot(cluster_with(), tmp_path, started=set())

    assert diagnostics.anomalies == []
    assert diagnostics.pairs[0].started is False
    assert diagnostics.pairs[0].down is False


@pytest.mark.parametrize(
    ("nodes", "detail"),
    [
        ([node("worker-01"), node("worker-02", ready="False")], "not Ready"),
        ([node("worker-01")], "gone from the cluster"),
    ],
)
def test_a_pods_node_that_is_not_ready_is_an_anomaly(
    tmp_path: Path, nodes: list[Manifest], detail: str
) -> None:
    diagnostics = snapshot(cluster_with(pod("client"), pod("server")), tmp_path, nodes=nodes)

    (anomaly,) = diagnostics.anomalies
    assert (anomaly.kind, anomaly.subject, anomaly.detail) == (
        AnomalyKind.NODE_NOT_READY,
        "worker-02",
        detail,
    )
    # The pair whose target it is, is down.
    assert diagnostics.pairs[0].down is True


def node_exporter_pod(node_name: str, **status: Any) -> Manifest:
    """One of the node-exporter DaemonSet's pods: run-owned, on every node, no test id."""
    manifest = pod("client", node_name=node_name, component="node-exporter", test_id="")
    manifest["metadata"]["name"] = f"run-{RUN}-node-exporter-{node_name}"
    del manifest["metadata"]["labels"][TEST_ID_LABEL]
    manifest["status"].update(status)
    return manifest


def prometheus_pod(**status: Any) -> Manifest:
    manifest = pod("server", node_name="worker-03", component="prometheus", test_id="")
    manifest["metadata"]["name"] = f"run-{RUN}-prometheus"
    del manifest["metadata"]["labels"][TEST_ID_LABEL]
    manifest["status"].update(status)
    return manifest


def test_the_node_exporters_pods_are_recorded_and_never_an_anomaly(tmp_path: Path) -> None:
    # 2026-10-15: it runs on every node, also those no test used. A node being drained
    # elsewhere in the cluster must not retain the run.
    restarted = [{"name": "node-exporter", "state": {"running": {}}, "restartCount": 3}]
    store = cluster_with(
        pod("client"),
        pod("server"),
        node_exporter_pod("worker-09", containerStatuses=restarted),
    )
    nodes = [*NODES, node("worker-09", ready="False")]

    diagnostics = snapshot(store, tmp_path, nodes=nodes)

    assert diagnostics.anomalies == []
    assert f"run-{RUN}-node-exporter-worker-09" in [p.name for p in diagnostics.pods]
    assert diagnostics.pairs[0].down is False


def test_the_prometheus_pods_health_is_checked(tmp_path: Path) -> None:
    restarted = [{"name": "prometheus", "state": {"running": {}}, "restartCount": 1}]
    store = cluster_with(pod("client"), pod("server"), prometheus_pod(containerStatuses=restarted))
    nodes = [node("worker-01"), node("worker-02"), node("worker-03", ready="False")]

    diagnostics = snapshot(store, tmp_path, nodes=nodes)

    assert sorted(kinds(diagnostics)) == [
        AnomalyKind.CONTAINER_RESTARTED,
        AnomalyKind.NODE_NOT_READY,
    ]
    # Neither is about the pair's pods or nodes.
    assert diagnostics.pairs[0].down is False


def test_an_oom_kill_before_a_restart_is_still_an_oom_kill(tmp_path: Path) -> None:
    container = {
        "name": "iperf2",
        "state": {"running": {}},
        "lastState": {"terminated": {"exitCode": 137, "reason": "OOMKilled"}},
        "restartCount": 1,
    }

    diagnostics = snapshot(
        cluster_with(pod("client", containers=[container]), pod("server")), tmp_path
    )

    assert sorted(kinds(diagnostics)) == [
        AnomalyKind.CONTAINER_OOM_KILLED,
        AnomalyKind.CONTAINER_RESTARTED,
    ]
    recorded = diagnostics.pods[0].containers[0]
    assert (recorded.last_state, recorded.last_reason, recorded.last_exit_code) == (
        "terminated",
        "OOMKilled",
        137,
    )


def test_the_report_keeps_why_the_cluster_ended_a_pod(tmp_path: Path) -> None:
    server = pod("server", phase="Failed", reason="Evicted")
    server["status"]["message"] = "The node was low on resource: memory."

    diagnostics = snapshot(cluster_with(pod("client"), server), tmp_path)

    (evicted,) = diagnostics.anomalies
    assert evicted.detail == "The node was low on resource: memory."
    recorded = next(p for p in diagnostics.pods if p.name.endswith("-server"))
    assert (recorded.reason, recorded.message) == (
        "Evicted",
        "The node was low on resource: memory.",
    )


class Unreachable(FakeResources):
    def list(self, kind: Kind, label_selector: str, namespace: str | None = None) -> list[Manifest]:
        raise ConnectionError("connection refused")


def test_an_unreachable_namespace_is_an_anomaly_and_every_started_pair_is_down(
    tmp_path: Path,
) -> None:
    diagnostics = snapshot(Unreachable(), tmp_path)

    assert kinds(diagnostics) == [AnomalyKind.NAMESPACE_UNREACHABLE]
    assert diagnostics.namespace_reachable is False
    assert diagnostics.pairs[0].down is True


@pytest.mark.parametrize("check", [lambda: False, lambda: 1 / 0])
def test_an_unreachable_prometheus_is_an_anomaly(tmp_path: Path, check: Any) -> None:
    diagnostics = snapshot(
        cluster_with(pod("client"), pod("server")), tmp_path, prometheus_ready=check
    )

    assert kinds(diagnostics) == [AnomalyKind.PROMETHEUS_UNREACHABLE]
    assert diagnostics.prometheus_reachable is False


def test_without_a_managed_prometheus_the_report_says_so(tmp_path: Path) -> None:
    document = json.loads(snapshot(cluster_with(pod("client"), pod("server")), tmp_path).to_json())

    # One type per field: a bool, or null where it was not checked.
    assert document["prometheus_deployed"] is False
    assert document["reachable"]["prometheus"] is None


def test_with_a_managed_prometheus_the_report_says_whether_it_was_up(tmp_path: Path) -> None:
    diagnostics = snapshot(
        cluster_with(pod("client"), pod("server")), tmp_path, prometheus_ready=lambda: True
    )
    document = json.loads(diagnostics.to_json())

    assert document["prometheus_deployed"] is True
    assert document["reachable"]["prometheus"] is True


# -- it never blocks cleanup --------------------------------------------------------------


def test_a_step_that_fails_is_recorded_and_the_others_still_run(tmp_path: Path) -> None:
    def broken() -> list[Manifest]:
        raise RuntimeError("the API server went away")

    diagnostics = capture(
        cluster_with(pod("client"), pod("server")),
        run_id=RUN,
        namespace=NAMESPACE,
        plan=PLAN,
        started={"pair-001"},
        artifacts=artifacts_in(tmp_path),
        list_nodes=broken,
        prometheus_ready=lambda: True,
    )

    assert diagnostics.complete is False
    (anomaly,) = diagnostics.anomalies
    assert (anomaly.kind, anomaly.subject) == (AnomalyKind.SNAPSHOT_INCOMPLETE, "nodes")
    assert "the API server went away" in anomaly.detail
    # ADR-05-07's consolidated view survives: the pods, the Prometheus and the pairs.
    assert len(diagnostics.pods) == 2
    assert diagnostics.prometheus_reachable is True
    assert [pair.test_id for pair in diagnostics.pairs] == ["pair-001"]


class Hanging(FakeResources):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def list(self, kind: Kind, label_selector: str, namespace: str | None = None) -> list[Manifest]:
        self.release.wait(10)
        return []


def test_a_snapshot_that_hangs_stops_at_its_own_timeout(tmp_path: Path) -> None:
    store = Hanging()
    started = time.monotonic()
    try:
        diagnostics = snapshot(store, tmp_path, timeout=0.2)
    finally:
        store.release.set()

    assert time.monotonic() - started < 5
    assert diagnostics.complete is False
    assert kinds(diagnostics) == [AnomalyKind.SNAPSHOT_INCOMPLETE]
    assert "not finished after 0.2s" in diagnostics.anomalies[0].detail


class Late(FakeResources):
    """Answers the pod list only once released, after the snapshot has timed out."""

    def __init__(self, *pods: Manifest) -> None:
        super().__init__()
        for manifest in pods:
            self.put(POD, NAMESPACE, manifest)
        self.release = threading.Event()
        self.answered = threading.Event()

    def list(self, kind: Kind, label_selector: str, namespace: str | None = None) -> list[Manifest]:
        self.release.wait(10)
        try:
            return super().list(kind, label_selector, namespace)
        finally:
            self.answered.set()


def test_the_report_returned_at_the_timeout_never_changes_after_it(tmp_path: Path) -> None:
    # Cleanup deletes pods right after the timeout: a late answer must not turn into
    # findings, nor make the log and diagnostics.json disagree.
    store = Late(pod("client"))
    diagnostics = snapshot(store, tmp_path, timeout=0.2)
    before = diagnostics.to_json()

    store.release.set()
    assert store.answered.wait(5)
    time.sleep(0.2)  # let the worker commit, if it still could

    assert diagnostics.to_json() == before
    assert diagnostics.complete is False
    assert diagnostics.pods == []
    assert kinds(diagnostics) == [AnomalyKind.SNAPSHOT_INCOMPLETE]


def test_a_worker_finishing_just_after_the_timeout_still_names_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The race at the timeout: the worker ends between close() and the check that follows
    # it. Its last commit is dropped, so the report is incomplete and must say why.
    import dokimi.diagnostics as module  # noqa: PLC0415

    release = threading.Event()
    close = module._Recorder.close

    def close_then_let_the_worker_finish(self: Any) -> Diagnostics:
        taken = close(self)
        release.set()
        for thread in threading.enumerate():
            if thread.name == "dokimi-diagnostics":
                thread.join(5)
        return taken

    monkeypatch.setattr(module._Recorder, "close", close_then_let_the_worker_finish)

    diagnostics = snapshot(
        cluster_with(pod("client"), pod("server")),
        tmp_path,
        prometheus_ready=lambda: release.wait(5),
        timeout=0.2,
    )

    assert diagnostics.complete is False
    assert kinds(diagnostics) == [AnomalyKind.SNAPSHOT_INCOMPLETE]
    assert diagnostics.retain is True


def test_its_timeout_fits_the_job_budgets_diagnostics_term() -> None:
    assert SNAPSHOT_TIMEOUT_SECONDS < DEFAULT_DIAGNOSTICS_SECONDS


# -- the report ---------------------------------------------------------------------------


def test_the_report_is_versioned_json_next_to_robots(tmp_path: Path) -> None:
    store = cluster_with(pod("client"), pod("server", phase="Failed", reason="Evicted"))

    path = write_report(snapshot(store, tmp_path), tmp_path)

    assert path == tmp_path / "diagnostics.json"
    document = json.loads(path.read_text())
    assert document["schema"] == SCHEMA_VERSION
    assert document["run_id"] == str(RUN)
    assert document["retain"] is True
    assert document["complete"] is True
    assert document["anomalies"] == [
        {"kind": "pod_evicted", "subject": f"run-{RUN}-pair-001-server", "detail": "Evicted"}
    ]
    assert document["prometheus_deployed"] is False
    assert {each["node"] for each in document["pods"]} == {"worker-01", "worker-02"}
    assert document["pairs"][0]["test_id"] == "pair-001"


def test_the_summary_counts_the_node_exporters_pods_rather_than_listing_them(
    tmp_path: Path,
) -> None:
    store = cluster_with(
        pod("client"),
        pod("server"),
        prometheus_pod(),
        *(node_exporter_pod(name) for name in ("worker-01", "worker-02", "worker-03")),
    )

    lines = snapshot(store, tmp_path).summary()

    assert lines[0] == "phase 10 (diagnostics): 6 pod(s), 1 pair(s), none down"
    assert f"  run-{RUN}-prometheus on worker-03: Running" in lines
    assert "  3 node-exporter pod(s)" in lines
    assert not any("node-exporter-worker" in line for line in lines)


def test_the_summary_names_what_is_down_and_why(tmp_path: Path) -> None:
    store = cluster_with(pod("client"), pod("server", phase="Failed", reason="Evicted"))

    lines = snapshot(store, tmp_path).summary()

    assert lines[0] == "phase 10 (diagnostics): 2 pod(s), 1 pair(s), down: pair-001"
    assert "  1 anomaly(ies): the run's resources are retained" in lines
    assert f"    pod_evicted run-{RUN}-pair-001-server: Evicted" in lines
