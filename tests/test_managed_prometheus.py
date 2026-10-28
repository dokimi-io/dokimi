"""The managed Prometheus: what it scrapes, what it is made of, and that it is kept.

Two of M1-08's acceptance criteria live here: the managed instance survives cleanup
unless the removal flag is passed, and exporter targets are discovered — the scrape
configuration selects exactly this run's exporter pods. The third, that an unqueryable
Prometheus fails phase 4 before any workload exists, is in ``test_cli.py``, through
``execute``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from dokimi.k8s.deadlines import DeadlineBudget, bound_pod
from dokimi.k8s.exclusivity import claim_run, mark_finished
from dokimi.k8s.ownership import (
    COMPONENT_LABEL,
    PROMETHEUS_COMPONENT,
    RUN_ID_LABEL,
    OwnershipError,
    RunOwnership,
    delete_run_owned,
    owner_manifest,
    owner_name,
    ownership_labels,
)
from dokimi.k8s.resources import CONFIG_MAP, DAEMON_SET, DEPLOYMENT, POD, SERVICE, Manifest
from dokimi.metrics.kept import find_kept_prometheus
from dokimi.metrics.managed import (
    EXPORTER_JOB,
    EXPORTER_PORT_NAME,
    NODE_EXPORTER_COMPONENT,
    NODE_EXPORTER_IMAGE,
    NODE_EXPORTER_PORT,
    NODE_EXPORTER_PORT_NAME,
    PROMETHEUS_IMAGE,
    RETENTION_SIZE,
    SCRAPE_INTERVAL_SECONDS,
    VOLUME_SIZE,
    NodeExporter,
    SupportingExporter,
    detect_supporting_exporters,
    managed_manifests,
    node_exporter_manifest,
    node_exporter_status,
    port_forward_command,
    provide_node_exporter,
    provision_managed_prometheus,
    removal_command,
    remove_node_exporter,
    render_config,
    select_monitoring,
)
from dokimi.model import ConfigError, MonitoringMode
from dokimi.run_identity import RunId
from dokimi.testtypes.iperf2 import Iperf2
from tests.fake_resources import FakeResources

RUN = RunId("20260924-143012-k3f9x2")
LATER_RUN = RunId("20260924-150000-b2c3d4")
NAMESPACE = "dokimi-iperf2"
NAME = f"run-{RUN}-prometheus"


def config(supporting: tuple[SupportingExporter, ...] = ()) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(render_config(str(RUN), NAMESPACE, supporting))
    return loaded


def job(name: str, supporting: tuple[SupportingExporter, ...] = ()) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = config(supporting)["scrape_configs"]
    (found,) = (job for job in jobs if job["job_name"] == name)
    return found


def provisioned(cluster: FakeResources, run: RunId = RUN) -> RunOwnership:
    cluster.put(CONFIG_MAP, NAMESPACE, owner_manifest(run, NAMESPACE))
    ownership = RunOwnership(cluster, run)
    provision_managed_prometheus(ownership, NAMESPACE)
    return ownership


# -- mode selection ----------------------------------------------------------------------


def test_managed_is_managed() -> None:
    selection = select_monitoring(MonitoringMode.MANAGED)

    assert selection.selected is MonitoringMode.MANAGED
    assert selection.describe() == "managed (requested)"


def test_auto_falls_back_to_managed_and_says_why() -> None:
    # ADR-03-02: auto's outcome is logged prominently, because the user did not choose it.
    selection = select_monitoring(MonitoringMode.AUTO)

    assert selection.selected is MonitoringMode.MANAGED
    assert "auto requested" in selection.describe()
    assert "falls back to managed" in selection.describe()


def test_existing_is_refused_rather_than_silently_replaced() -> None:
    with pytest.raises(ConfigError, match="'existing' is not available"):
        select_monitoring(MonitoringMode.EXISTING)


# -- what it scrapes ---------------------------------------------------------------------


def test_it_scrapes_this_runs_exporter_pods_only() -> None:
    # A kept Prometheus from an earlier run must never record a later run's results.
    (discovery,) = job(EXPORTER_JOB)["kubernetes_sd_configs"]

    assert discovery["role"] == "pod"
    assert discovery["namespaces"] == {"names": [NAMESPACE]}
    assert discovery["selectors"] == [
        {"role": "pod", "label": f"app.kubernetes.io/managed-by=dokimi,{RUN_ID_LABEL}={RUN}"}
    ]


def test_an_exporter_is_a_container_port_named_metrics() -> None:
    assert job(EXPORTER_JOB)["relabel_configs"] == [
        {
            "source_labels": ["__meta_kubernetes_pod_container_port_name"],
            "regex": EXPORTER_PORT_NAME,
            "action": "keep",
        }
    ]


def test_the_exporters_labels_win_over_target_labels() -> None:
    # The labels are the metric contract (ADR-02-04); Prometheus must not rewrite them.
    assert job(EXPORTER_JOB)["honor_labels"] is True


def test_the_iperf2_exporter_names_its_port_the_way_the_scrape_looks_for_it() -> None:
    assert Iperf2().exporter().port_name == EXPORTER_PORT_NAME


def test_the_scrape_interval_is_ours_and_recorded() -> None:
    # ADR-03-01: in managed mode we set it, so it is known and goes in the run metadata.
    interval = f"{SCRAPE_INTERVAL_SECONDS}s"
    assert config()["global"]["scrape_interval"] == interval

    ownership = RunOwnership(FakeResources(), RUN)
    metadata = provision_managed_prometheus(ownership, NAMESPACE).run_metadata()
    assert metadata["prometheus_scrape_interval_seconds"] == str(SCRAPE_INTERVAL_SECONDS)
    assert metadata["monitoring_mode"] == "managed"
    assert metadata["prometheus_image"] == PROMETHEUS_IMAGE


def test_a_supporting_exporter_is_scraped_where_it_runs() -> None:
    exporter = SupportingExporter("node-exporter", "monitoring", "prometheus-node-exporter")

    scrape = job("node-exporter", (exporter,))

    assert scrape["kubernetes_sd_configs"] == [
        {"role": "endpointslice", "namespaces": {"names": ["monitoring"]}}
    ]
    assert scrape["relabel_configs"][0]["regex"] == "prometheus-node-exporter"


def test_no_supporting_exporter_means_no_supporting_job() -> None:
    assert [job["job_name"] for job in config()["scrape_configs"]] == [EXPORTER_JOB]


# -- detecting supporting exporters ------------------------------------------------------


def service(namespace: str, name: str, app: str, **labels: str) -> Manifest:
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": app, **labels},
        }
    }


def test_already_present_exporters_are_found_and_reused() -> None:
    cluster = FakeResources(namespaces={"monitoring", "kube-system", NAMESPACE})
    cluster.put(SERVICE, "monitoring", service("monitoring", "node-exporter", "node-exporter"))
    cluster.put(
        SERVICE, "kube-system", service("kube-system", "kube-state-metrics", "kube-state-metrics")
    )

    assert detect_supporting_exporters(cluster) == [
        SupportingExporter("node-exporter", "monitoring", "node-exporter"),
        SupportingExporter("kube-state-metrics", "kube-system", "kube-state-metrics"),
    ]


def test_the_helm_chart_name_counts_as_a_node_exporter() -> None:
    cluster = FakeResources(namespaces={"monitoring"})
    cluster.put(
        SERVICE,
        "monitoring",
        service("monitoring", "kps-prometheus-node-exporter", "prometheus-node-exporter"),
    )

    (found,) = detect_supporting_exporters(cluster)
    assert found.kind == "node-exporter"


def test_an_absent_kube_state_metrics_is_recorded_as_absent_not_deployed() -> None:
    cluster = FakeResources()

    assert detect_supporting_exporters(cluster) == []
    metadata = provision_managed_prometheus(RunOwnership(cluster, RUN), NAMESPACE).run_metadata()
    assert metadata["supporting_kube_state_metrics"] == ""
    assert not cluster.list(DEPLOYMENT, "app.kubernetes.io/name=kube-state-metrics")


def test_our_own_objects_are_never_a_supporting_exporter() -> None:
    cluster = FakeResources(namespaces={NAMESPACE})
    cluster.put(
        SERVICE,
        NAMESPACE,
        service(
            NAMESPACE, "impostor", "node-exporter", **{"app.kubernetes.io/managed-by": "dokimi"}
        ),
    )

    assert detect_supporting_exporters(cluster) == []


# -- what it is made of ------------------------------------------------------------------


def test_it_is_a_config_map_a_deployment_and_a_service() -> None:
    kinds = [kind for kind, _ in managed_manifests(str(RUN), NAMESPACE)]

    assert kinds == [CONFIG_MAP, DEPLOYMENT, SERVICE]


def test_it_runs_minimal_and_non_root_as_its_own_account() -> None:
    (_, deployment) = managed_manifests(str(RUN), NAMESPACE)[1]
    pod = deployment["spec"]["template"]["spec"]
    (container,) = pod["containers"]

    assert pod["serviceAccountName"] == "dokimi-prometheus"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["image"] == PROMETHEUS_IMAGE


def test_the_image_is_pinned_by_digest() -> None:
    assert "@sha256:" in PROMETHEUS_IMAGE
    assert len(PROMETHEUS_IMAGE.partition("@sha256:")[2]) == 64


def test_the_service_selects_only_this_runs_prometheus() -> None:
    (_, deployment), (_, svc) = managed_manifests(str(RUN), NAMESPACE)[1:]

    assert svc["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]
    assert svc["spec"]["selector"][RUN_ID_LABEL] == str(RUN)


def test_the_endpoint_is_the_services_cluster_dns_name() -> None:
    prometheus = provision_managed_prometheus(RunOwnership(FakeResources(), RUN), NAMESPACE)

    assert prometheus.endpoint == f"http://{NAME}.{NAMESPACE}.svc:9090"


# -- kept by default ---------------------------------------------------------------------


def test_it_is_run_labelled_but_owned_by_nothing() -> None:
    # Labelled, so it stays attributable and removable by run_id; no ownerReference, so
    # deleting the run's owner does not cascade to it (ADR-03-02, ADR-05-02's exception).
    cluster = FakeResources()
    provisioned(cluster)

    for kind in (CONFIG_MAP, DEPLOYMENT, SERVICE):
        stored = cluster.get(kind, NAMESPACE, NAME)
        assert stored["metadata"]["labels"][RUN_ID_LABEL] == str(RUN)
        assert stored["metadata"]["labels"][COMPONENT_LABEL] == PROMETHEUS_COMPONENT
        assert "ownerReferences" not in stored["metadata"]


def test_it_survives_cleanup_by_default() -> None:
    cluster = FakeResources()
    ownership = provisioned(cluster)
    budget = DeadlineBudget(
        test_duration=1, startup=1, warmup=0, direct_read=1, scrape_confirmation=1
    )
    workload = {"kind": "Pod", "metadata": {"name": "client", "namespace": NAMESPACE}}
    ownership.create(POD, bound_pod(workload, budget), test_id="pair-001")

    report = delete_run_owned(cluster, RUN)

    assert report.complete
    assert not cluster.exists(POD, NAMESPACE, "client")
    for kind in (CONFIG_MAP, DEPLOYMENT, SERVICE):
        assert cluster.exists(kind, NAMESPACE, NAME)
    assert {str(obj) for obj in report.kept} == {
        f"ConfigMap/{NAME} in {NAMESPACE}",
        f"Deployment/{NAME} in {NAMESPACE}",
        f"Service/{NAME} in {NAMESPACE}",
    }


def test_the_removal_flag_removes_it() -> None:
    cluster = FakeResources()
    provisioned(cluster)

    report = delete_run_owned(cluster, RUN, remove_kept=True)

    assert report.complete and report.kept == []
    for kind in (CONFIG_MAP, DEPLOYMENT, SERVICE):
        assert not cluster.exists(kind, NAMESPACE, NAME)


def test_a_kept_prometheus_does_not_block_the_next_run() -> None:
    # ADR-05-13: exclusivity reads run owners only, and a Prometheus is not an owner.
    cluster = FakeResources()
    provisioned(cluster)
    delete_run_owned(cluster, RUN)
    cluster.put(CONFIG_MAP, NAMESPACE, owner_manifest(LATER_RUN, NAMESPACE))

    claim_run(
        cluster, LATER_RUN, namespace=NAMESPACE, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )


def test_the_removal_command_selects_by_label_not_by_name() -> None:
    prometheus = provision_managed_prometheus(RunOwnership(FakeResources(), RUN), NAMESPACE)

    assert removal_command(prometheus) == (
        f"kubectl delete deployment,service,configmap -n {NAMESPACE} "
        f"-l {RUN_ID_LABEL}={RUN},{COMPONENT_LABEL}={PROMETHEUS_COMPONENT}"
    )


def test_only_a_kept_component_may_be_created_unowned() -> None:
    ownership = RunOwnership(FakeResources(), RUN)
    manifest = {"kind": "ConfigMap", "metadata": {"name": "x", "namespace": NAMESPACE}}

    with pytest.raises(OwnershipError, match="not a component cleanup keeps"):
        ownership.create_kept(CONFIG_MAP, manifest, component="server")


def test_a_workload_is_never_kept() -> None:
    ownership = RunOwnership(FakeResources(), RUN)
    pod = {"kind": "Pod", "metadata": {"name": "x", "namespace": NAMESPACE}}

    with pytest.raises(OwnershipError, match="cannot be kept"):
        ownership.create_kept(POD, pod, component=PROMETHEUS_COMPONENT)


# -- off the test's nodes, and bounded ---------------------------------------------------


def pod_spec(**kwargs: Any) -> dict[str, Any]:
    (_, deployment) = managed_manifests(str(RUN), NAMESPACE, **kwargs)[1]
    spec: dict[str, Any] = deployment["spec"]["template"]["spec"]
    return spec


def test_it_prefers_any_node_but_the_tested_ones() -> None:
    affinity = pod_spec(avoid_nodes=["worker-02", "worker-01", "worker-02"])["affinity"]

    terms = affinity["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    assert [term["preference"]["matchFields"] for term in terms] == [
        [{"key": "metadata.name", "operator": "NotIn", "values": ["worker-01"]}],
        [{"key": "metadata.name", "operator": "NotIn", "values": ["worker-02"]}],
    ]


def test_each_node_field_term_holds_one_value() -> None:
    # The API server refuses a node field selector with more than one value for In or
    # NotIn (HTTP 422, found on kind), and the fake API server does not validate it.
    affinity = pod_spec(avoid_nodes=["worker-01", "worker-02", "worker-03"])["affinity"]

    terms = affinity["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    assert len(terms) == 3
    for term in terms:
        for field in term["preference"]["matchFields"]:
            assert len(field["values"]) == 1


def test_avoiding_the_tested_nodes_is_preferred_never_required() -> None:
    # A two-node cluster has nowhere else to put it, and must still run.
    affinity = pod_spec(avoid_nodes=["worker-01", "worker-02"])["affinity"]["nodeAffinity"]

    assert "requiredDuringSchedulingIgnoredDuringExecution" not in affinity


def test_no_tested_nodes_means_no_affinity() -> None:
    assert "affinity" not in pod_spec()


def test_what_a_kept_prometheus_holds_is_bounded() -> None:
    spec = pod_spec()
    (container,) = spec["containers"]

    assert f"--storage.tsdb.retention.size={RETENTION_SIZE}" in container["args"]
    assert {"name": "data", "emptyDir": {"sizeLimit": VOLUME_SIZE}} in spec["volumes"]


def test_the_metadata_records_the_requested_mode_and_why_it_changed() -> None:
    prometheus = provision_managed_prometheus(
        RunOwnership(FakeResources(), RUN),
        NAMESPACE,
        selection=select_monitoring(MonitoringMode.AUTO),
    )

    metadata = prometheus.run_metadata()
    assert metadata["monitoring_mode"] == "managed"
    assert metadata["monitoring_mode_requested"] == "auto"
    assert "falls back to managed" in metadata["monitoring_mode_reason"]


# -- the node-exporter -------------------------------------------------------------------


def owned(cluster: FakeResources, run: RunId = RUN) -> RunOwnership:
    cluster.put(CONFIG_MAP, NAMESPACE, owner_manifest(run, NAMESPACE))
    return RunOwnership(cluster, run)


def daemon_set_spec() -> dict[str, Any]:
    spec: dict[str, Any] = node_exporter_manifest(str(RUN), NAMESPACE)["spec"]["template"]["spec"]
    return spec


def test_an_absent_node_exporter_is_deployed_and_owned_by_the_run() -> None:
    cluster = FakeResources()

    exporter = provide_node_exporter(owned(cluster), NAMESPACE)

    assert exporter == NodeExporter(NAMESPACE, f"run-{RUN}-node-exporter", str(RUN), True)
    stored = cluster.get(DAEMON_SET, NAMESPACE, f"run-{RUN}-node-exporter")
    assert stored["metadata"]["labels"][COMPONENT_LABEL] == NODE_EXPORTER_COMPONENT
    assert stored["metadata"]["ownerReferences"]


def test_a_present_node_exporter_means_none_is_deployed() -> None:
    cluster = FakeResources()
    present = [SupportingExporter("node-exporter", "monitoring", "node-exporter")]

    assert provide_node_exporter(owned(cluster), NAMESPACE, present) is None
    assert not cluster.list(DAEMON_SET, f"{COMPONENT_LABEL}={NODE_EXPORTER_COMPONENT}")


def test_an_earlier_runs_node_exporter_is_reused_never_duplicated() -> None:
    # On the host's network a second DaemonSet could never bind the port.
    cluster = FakeResources()
    provide_node_exporter(owned(cluster), NAMESPACE)

    exporter = provide_node_exporter(owned(cluster, LATER_RUN), NAMESPACE)

    assert exporter == NodeExporter(NAMESPACE, f"run-{RUN}-node-exporter", str(RUN), False)
    assert str(exporter) == f"deployed by run {RUN}, reused ({NAMESPACE}/run-{RUN}-node-exporter)"
    assert len(cluster.list(DAEMON_SET, f"{COMPONENT_LABEL}={NODE_EXPORTER_COMPONENT}")) == 1


def test_a_run_removes_the_node_exporter_it_deployed() -> None:
    # The 2026-10-07 decision: a run cleans up what it created.
    cluster = FakeResources()
    exporter = provide_node_exporter(owned(cluster), NAMESPACE)
    assert exporter is not None

    assert remove_node_exporter(cluster, exporter) is True
    assert cluster.list(DAEMON_SET, f"{COMPONENT_LABEL}={NODE_EXPORTER_COMPONENT}") == []


def test_a_run_leaves_a_node_exporter_it_found() -> None:
    # One a crashed run left behind is used and left, like the cluster's own.
    cluster = FakeResources()
    provide_node_exporter(owned(cluster), NAMESPACE)
    exporter = provide_node_exporter(owned(cluster, LATER_RUN), NAMESPACE)
    assert exporter is not None

    assert remove_node_exporter(cluster, exporter) is False
    assert len(cluster.list(DAEMON_SET, f"{COMPONENT_LABEL}={NODE_EXPORTER_COMPONENT}")) == 1


def test_a_node_exporter_already_gone_is_not_an_error() -> None:
    cluster = FakeResources()
    exporter = provide_node_exporter(owned(cluster), NAMESPACE)
    assert exporter is not None
    remove_node_exporter(cluster, exporter)

    assert remove_node_exporter(cluster, exporter) is False


def test_a_kept_prometheus_is_reached_through_a_port_forward() -> None:
    prometheus = provision_managed_prometheus(owned(FakeResources()), NAMESPACE)

    assert port_forward_command(prometheus) == (
        f"dokimi prometheus {prometheus.run_id} --namespace {NAMESPACE}"
    )


def test_the_node_exporter_asks_for_the_host_network_and_nothing_else() -> None:
    spec = daemon_set_spec()
    (container,) = spec["containers"]

    assert spec["hostNetwork"] is True
    assert "hostPID" not in spec and "hostIPC" not in spec
    assert "volumes" not in spec
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def test_the_node_exporter_collects_cpu_memory_and_network_interfaces_only() -> None:
    (container,) = daemon_set_spec()["containers"]
    collectors = [arg for arg in container["args"] if arg.startswith("--collector.")]

    assert collectors == [
        "--collector.disable-defaults",
        "--collector.cpu",
        "--collector.meminfo",
        "--collector.netdev",
    ]


def test_the_node_exporter_port_is_neither_a_node_exporters_nor_the_test_exporters() -> None:
    (container,) = daemon_set_spec()["containers"]
    (port,) = container["ports"]

    assert NODE_EXPORTER_PORT not in (9100, Iperf2().exporter().port)
    assert f"--web.listen-address=:{NODE_EXPORTER_PORT}" in container["args"]
    # Named apart from the run's exporters, or their job would scrape it as well.
    assert port == {"name": NODE_EXPORTER_PORT_NAME, "containerPort": NODE_EXPORTER_PORT}
    assert NODE_EXPORTER_PORT_NAME != EXPORTER_PORT_NAME


def test_the_node_exporter_image_is_pinned_by_digest() -> None:
    (container,) = daemon_set_spec()["containers"]

    assert container["image"] == NODE_EXPORTER_IMAGE
    assert len(NODE_EXPORTER_IMAGE.partition("@sha256:")[2]) == 64


def test_a_deployed_node_exporter_is_scraped_by_its_labels_and_port() -> None:
    exporter = NodeExporter(NAMESPACE, f"run-{RUN}-node-exporter", str(RUN), True)
    loaded = yaml.safe_load(render_config(str(RUN), NAMESPACE, (), exporter))
    (found,) = (job for job in loaded["scrape_configs"] if job["job_name"] == "node-exporter")

    (discovery,) = found["kubernetes_sd_configs"]
    assert discovery["namespaces"] == {"names": [NAMESPACE]}
    assert discovery["selectors"][0]["label"] == (
        f"app.kubernetes.io/managed-by=dokimi,{COMPONENT_LABEL}={NODE_EXPORTER_COMPONENT},"
        f"dokimi.io/run-id={RUN}"
    )
    assert found["relabel_configs"][0] == {
        "source_labels": ["__meta_kubernetes_pod_container_port_name"],
        "regex": NODE_EXPORTER_PORT_NAME,
        "action": "keep",
    }


def test_the_metadata_names_the_node_exporter_the_run_deployed() -> None:
    cluster = FakeResources()
    ownership = owned(cluster)
    exporter = provide_node_exporter(ownership, NAMESPACE)

    metadata = provision_managed_prometheus(
        ownership, NAMESPACE, node_exporter=exporter
    ).run_metadata()

    assert metadata["supporting_node_exporter"] == (
        f"deployed by this run ({NAMESPACE}/run-{RUN}-node-exporter)"
    )


def test_a_deployed_node_exporter_is_removed_with_the_run_and_the_prometheus_kept() -> None:
    cluster = FakeResources()
    ownership = owned(cluster)
    exporter = provide_node_exporter(ownership, NAMESPACE)
    provision_managed_prometheus(ownership, NAMESPACE, node_exporter=exporter)

    report = delete_run_owned(cluster, RUN)

    assert report.complete
    assert not cluster.exists(DAEMON_SET, NAMESPACE, f"run-{RUN}-node-exporter")
    assert cluster.exists(DEPLOYMENT, NAMESPACE, NAME)


def with_status(cluster: FakeResources, **status: int) -> NodeExporter:
    exporter = provide_node_exporter(owned(cluster), NAMESPACE)
    assert exporter is not None
    stored = cluster.get(DAEMON_SET, NAMESPACE, exporter.name)
    cluster.replace(DAEMON_SET, NAMESPACE, {**stored, "status": status})
    return exporter


def test_node_exporter_pods_that_were_never_created_are_reported_not_fatal() -> None:
    # Pod Security refuses the pods, not the DaemonSet: only its status shows it.
    cluster = FakeResources()
    exporter = with_status(cluster, desiredNumberScheduled=2, currentNumberScheduled=0)

    running, summary = node_exporter_status(cluster, exporter)

    assert running is False
    assert "only 0 of 2 pods created" in summary
    assert "Pod Security" in summary


def test_running_node_exporter_pods_are_counted() -> None:
    cluster = FakeResources()
    exporter = with_status(
        cluster, desiredNumberScheduled=3, currentNumberScheduled=3, numberReady=2
    )

    assert node_exporter_status(cluster, exporter) == (True, "2 of 3 pods ready")


def test_a_status_not_reported_yet_is_said_so_rather_than_guessed() -> None:
    cluster = FakeResources()
    exporter = with_status(cluster)

    assert node_exporter_status(cluster, exporter) == (True, "pods not reported yet")


# -- the kept Prometheus, after the run ------------------------------------------------------


def prometheus_pod(
    cluster: FakeResources, name: str, *, ready: bool = True, run: RunId = RUN
) -> None:
    """The pod the Prometheus Deployment would run: the fake has no controller."""
    status = "True" if ready else "False"
    cluster.put(
        POD,
        NAMESPACE,
        {
            "metadata": {
                "name": name,
                "labels": ownership_labels(run, component=PROMETHEUS_COMPONENT),
            },
            "status": {
                "phase": "Running" if ready else "Pending",
                "conditions": [{"type": "Ready", "status": status}],
            },
        },
    )


def test_a_kept_prometheus_is_found_by_its_runs_labels() -> None:
    cluster = FakeResources()
    provision_managed_prometheus(owned(cluster), NAMESPACE)
    prometheus_pod(cluster, f"{NAME}-7d9f6c5b8-x2kqp")
    finished = datetime(2026, 9, 24, 14, 33, 40, tzinfo=UTC)
    mark_finished(cluster, RUN, namespace=NAMESPACE, now=lambda: finished)

    kept = find_kept_prometheus(cluster, RUN, NAMESPACE)

    assert kept is not None
    assert kept.pod == f"{NAME}-7d9f6c5b8-x2kqp"
    assert kept.problem is None
    assert kept.started_at == datetime(2026, 9, 24, 14, 30, 12, tzinfo=UTC)
    assert kept.finished_at == finished


def test_a_removed_prometheus_is_none() -> None:
    # Removed with `dokimi cleanup --prometheus`, or never deployed: the cluster's own
    # Prometheus was used.
    cluster = FakeResources()
    owned(cluster)

    assert find_kept_prometheus(cluster, RUN, NAMESPACE) is None


def test_another_runs_prometheus_is_never_found() -> None:
    cluster = FakeResources()
    other = RunId("20260924-150000-zzzzzz")
    provision_managed_prometheus(owned(cluster, other), NAMESPACE)
    prometheus_pod(cluster, "theirs", run=other)
    owned(cluster)

    assert find_kept_prometheus(cluster, RUN, NAMESPACE) is None


def test_a_prometheus_without_a_ready_pod_says_why() -> None:
    cluster = FakeResources()
    provision_managed_prometheus(owned(cluster), NAMESPACE)
    prometheus_pod(cluster, "starting", ready=False)

    kept = find_kept_prometheus(cluster, RUN, NAMESPACE)

    assert kept is not None
    assert kept.pod is None
    assert kept.problem == "none of its pods is ready (starting Pending)"


def test_a_run_whose_owner_is_gone_has_no_recorded_end() -> None:
    # A run that kept nothing loses its owner once its reports are fetched; its
    # Prometheus stays, and its start is still in its id.
    cluster = FakeResources()
    provision_managed_prometheus(owned(cluster), NAMESPACE)
    prometheus_pod(cluster, "ready")
    owner = cluster.get(CONFIG_MAP, NAMESPACE, owner_name(RUN))
    cluster.delete(CONFIG_MAP, NAMESPACE, owner_name(RUN), uid=owner["metadata"]["uid"])

    kept = find_kept_prometheus(cluster, RUN, NAMESPACE)

    assert kept is not None
    assert (kept.finished_at, kept.owner_found) == (None, False)
    assert kept.started_at == RUN.started_at
