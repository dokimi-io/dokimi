"""The CLI: what each command does, and what it exits with.

Exit codes are automation-facing from the first release (ADR-01-08), so they are asserted
as values rather than as "non-zero".
"""

from __future__ import annotations

import contextlib
import json
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from dokimi import _image_digest, images
from dokimi.cleanup import test_pods
from dokimi.cli import commands
from dokimi.cli.exit_codes import ExitCode
from dokimi.cli.main import main
from dokimi.error_categories import ErrorCategory
from dokimi.formats import parse_file
from dokimi.images import IMAGE_REPOSITORY
from dokimi.k8s.cluster import ClusterError
from dokimi.k8s.exclusivity import claim_run
from dokimi.k8s.manifests import resource_name
from dokimi.k8s.ownership import (
    CLAIM_EXPIRES_AT_ANNOTATION,
    CLAIMED_AT_ANNOTATION,
    FINISHED_AT_ANNOTATION,
    RETAINED_ANNOTATION,
    owner_manifest,
    owner_name,
    ownership_labels,
)
from dokimi.k8s.port_forward import PortRelay
from dokimi.k8s.resources import CONFIG_MAP, DAEMON_SET, DEPLOYMENT, JOB, POD
from dokimi.measurement import Measurement
from dokimi.metrics.prometheus import PrometheusClient, Response
from dokimi.model import Comparison, Criterion, Metric, Unit
from dokimi.run_identity import RunId
from dokimi.run_images import COMPONENTS
from dokimi.testtypes.interface import Role
from dokimi.testtypes.probe import ProbeCheck, ProbeResult
from dokimi.validation import ExpectedPair, TestError, evaluate
from tests.fake_cluster import ExplodingCluster, FakeCluster, node
from tests.fake_probe import FakeProbe
from tests.fake_robot import FakeRobotRunner

DIGEST = "sha256:" + "ab" * 32
IMAGE = f"{IMAGE_REPOSITORY}@{DIGEST}"
RUN_ID = "20260101-100000-4f2a9c"
#: When the cluster started the Job ``submit`` created for a run, in the Job's status.
JOB_STARTED = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "worker-01-to-worker-02-udp.yaml"
#: What the Job's manifest passes to the in-cluster entry point.
EXECUTE = [
    "execute",
    "--definition",
    str(EXAMPLE),
    "--run-id",
    RUN_ID,
    "--namespace",
    "dokimi-iperf2",
    "--deadline-seconds",
    "3600",
]


@pytest.fixture(autouse=True)
def _released_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most commands need images to submit and deploy; a checkout has none by design."""
    # And a cluster name, which has no default either (ADR-02-07): on the submitter's side
    # as an installation override, and in the Job as what submit wrote.
    monkeypatch.setenv("DOKIMI_CLUSTER", "test-cluster")
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", DIGEST)
    monkeypatch.setattr(_image_digest, "IPERF2_IMAGE_DIGEST", DIGEST)
    for component in COMPONENTS.values():
        for name in (component.env_var, component.source_env_var, *component.env_aliases):
            monkeypatch.delenv(name, raising=False)


def given_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """What ``submit`` writes into the Job's environment: each image and its source."""
    for component in COMPONENTS.values():
        monkeypatch.setenv(component.env_var, f"ghcr.io/dokimi-io/{component.name}@{DIGEST}")
        monkeypatch.setenv(component.source_env_var, "release-default")


#: One node-exporter target, scraped.
SCRAPED: dict[str, object] = {
    "scrapePool": "node-exporter",
    "health": "up",
    "labels": {"node": "worker-01"},
}


class FakePrometheus:
    """The run's Prometheus over the real client, answering from a script, offline."""

    def __init__(self, *, answers: bool = True) -> None:
        self.answers = answers
        self.urls: list[str] = []
        self.targets: list[dict[str, object]] = [SCRAPED]
        #: What a query for a test's series returns: Prometheus's vector, as JSON.
        self.series: list[dict[str, Any]] = []

    def transport(self, url: str) -> Response:
        self.urls.append(url)
        if not self.answers:
            raise ConnectionRefusedError("connection refused")
        if url.endswith("/-/ready"):
            return Response(200, b"Prometheus Server is Ready.")
        if "/api/v1/targets" in url:
            data = {"activeTargets": self.targets}
            return Response(200, json.dumps({"status": "success", "data": data}).encode())
        result = self.series if "dokimi_" in urllib.parse.unquote(url) else []
        body = {"status": "success", "data": {"resultType": "vector", "result": result}}
        return Response(200, json.dumps(body).encode())

    def client(self, endpoint: str) -> PrometheusClient:
        return PrometheusClient(endpoint, transport=self.transport)


@pytest.fixture(autouse=True)
def prometheus(monkeypatch: pytest.MonkeyPatch) -> FakePrometheus:
    """Every execute talks to its managed Prometheus; none of them reaches a network."""
    fake = FakePrometheus()
    monkeypatch.setattr(commands, "PrometheusClient", fake.client)
    return fake


def submitted(fake: FakeCluster, run_id: str, namespace: str = "dokimi-iperf2") -> None:
    """What ``submit`` leaves for the Job to find: the run's owner, and its started Job."""
    fake.store.put(CONFIG_MAP, namespace, owner_manifest(RunId(run_id), namespace))
    fake.store.put(
        JOB,
        namespace,
        {
            "metadata": {"name": resource_name(run_id), "labels": ownership_labels(RunId(run_id))},
            "status": {"startTime": JOB_STARTED.isoformat().replace("+00:00", "Z")},
        },
    )


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    fake = FakeCluster(log_lines=["run started", "run finished"], exit_code=0)
    monkeypatch.setattr(commands, "connect", lambda **_kwargs: fake)
    return fake


@pytest.fixture
def job_cluster(cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch) -> FakeCluster:
    """The cluster as the Job sees it: its run already submitted, with its images."""
    submitted(cluster, RUN_ID)
    given_images(monkeypatch)
    return cluster


@pytest.fixture(autouse=True)
def robot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRobotRunner:
    """Phase 6's runner, faked, and the Job's artifact directory, in a temporary one."""
    fake = FakeRobotRunner()
    monkeypatch.setattr(commands, "ROBOT_RUNNER", lambda: fake)
    monkeypatch.setattr(commands, "ARTIFACTS_ROOT", tmp_path / "artifacts")
    return fake


@pytest.fixture(autouse=True)
def probe(monkeypatch: pytest.MonkeyPatch) -> FakeProbe:
    """Phase 4's probe, with pods that are Ready as soon as they exist, and pass."""
    fake = FakeProbe()
    monkeypatch.setattr(commands, "PREFLIGHT_PLUGIN", fake)
    return fake


@pytest.fixture
def no_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_kwargs: ExplodingCluster())


# -- validate -----------------------------------------------------------------


def test_validate_accepts_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(EXAMPLE)]) == ExitCode.PASS
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "worker-01 -> worker-02" in out


def test_validate_rejects_a_malformed_definition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("apiVersion: dokimi.io/v1alpha1\nkind: NetworkTest\n")

    assert main(["validate", str(broken)]) == ExitCode.CONFIG_ERROR
    assert capsys.readouterr().err.startswith("dokimi: ")


def test_a_missing_file_is_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", "/nonexistent/test.yaml"]) == ExitCode.CONFIG_ERROR
    assert "no such file" in capsys.readouterr().err


def test_an_unreadable_format_is_a_config_error(tmp_path: Path) -> None:
    unknown = tmp_path / "test.txt"
    unknown.write_text("whatever")

    assert main(["validate", str(unknown)]) == ExitCode.CONFIG_ERROR


# -- dry run ------------------------------------------------------------------


def test_dry_run_renders_the_matrix_and_the_manifests(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.PASS

    captured = capsys.readouterr()
    # Manifests on stdout, everything a person reads on stderr, so the rendering can be
    # saved or diffed without a summary mixed into it.
    assert "kind: Job" in captured.out
    assert "kind: ConfigMap" in captured.out
    assert "Planned matrix (1 test, sequential" in captured.err
    assert "worker-01 -> worker-02" in captured.err
    assert "nothing was created" in captured.err


def test_dry_run_touches_no_cluster(no_cluster: None) -> None:
    # The fixture's client raises on contact, so this passing is the assertion: not one
    # call was made. ADR-01-06 requires --dry-run to stay fully local.
    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.PASS


def test_dry_run_rejects_invalid_input_before_anything_else(
    no_cluster: None, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: NetworkTest\n")

    assert main(["submit", str(broken), "--dry-run"]) == ExitCode.CONFIG_ERROR


def test_invalid_input_never_reaches_the_cluster(no_cluster: None, tmp_path: Path) -> None:
    # Same client that raises on contact, but this time without --dry-run: validation
    # must fail first (ADR-05-05's config_error, detected before deployment).
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: NetworkTest\n")

    assert main(["submit", str(broken)]) == ExitCode.CONFIG_ERROR


def test_a_development_build_has_no_image_to_submit(
    no_cluster: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", None)

    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.CONFIG_ERROR
    assert "development build" in capsys.readouterr().err


# -- submit -------------------------------------------------------------------


def test_submit_creates_the_run_and_streams_it(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--run-id", RUN_ID]) == ExitCode.PASS

    captured = capsys.readouterr()
    assert cluster.created_kinds == ["ConfigMap", "ConfigMap", "Job"]
    assert "run started" in captured.out
    assert f"submitted run {RUN_ID}" in captured.err


def test_submit_propagates_the_runs_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster(exit_code=ExitCode.FAIL))

    assert main(["submit", str(EXAMPLE)]) == ExitCode.FAIL


def test_no_follow_returns_immediately_with_a_way_back(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--run-id", RUN_ID, "--no-follow"]) == ExitCode.PASS

    assert cluster.streamed == []
    assert f"attach {RUN_ID}" in capsys.readouterr().err


def test_submit_honours_the_namespace_override(cluster: FakeCluster) -> None:
    main(["submit", str(EXAMPLE), "--namespace", "somewhere-else"])

    assert {manifest["metadata"]["namespace"] for manifest in cluster.created} == {"somewhere-else"}


def test_a_malformed_namespace_is_a_config_error(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # ADR-01-11: an invalid request exits 3 before anything is created, not 5.
    assert main(["submit", str(EXAMPLE), "--namespace", "Not_Valid"]) == ExitCode.CONFIG_ERROR
    assert "not a valid namespace name" in capsys.readouterr().err


# -- attach -------------------------------------------------------------------


def test_attach_resumes_streaming_a_run_it_never_submitted(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["attach", RUN_ID]) == ExitCode.PASS

    assert cluster.created == [], "attaching must not create anything"
    assert "run finished" in capsys.readouterr().out


def test_attach_needs_only_a_run_id_and_cluster_access(cluster: FakeCluster) -> None:
    # Nothing local is consulted: no definition, no state file, no record of the
    # submitting process. That is what makes reattach work from another machine.
    assert main(["attach", RUN_ID]) == ExitCode.PASS
    assert cluster.streamed


def test_attach_propagates_the_runs_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster(exit_code=ExitCode.ERROR))

    assert main(["attach", RUN_ID]) == ExitCode.ERROR


def test_attach_rejects_a_malformed_run_id(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["attach", "yesterdays-run"]) == ExitCode.CONFIG_ERROR
    assert "malformed run id" in capsys.readouterr().err


def test_attach_reports_a_run_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster(has_pod=False, has_job=False))

    assert main(["attach", RUN_ID]) == ExitCode.CONFIG_ERROR
    assert "no run" in capsys.readouterr().err


def test_interrupting_leaves_the_run_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster())
    monkeypatch.setattr(commands, "follow", interrupt)

    assert main(["attach", RUN_ID]) == ExitCode.INTERRUPTED
    err = capsys.readouterr().err
    assert "continues in the cluster" in err
    assert f"attach {RUN_ID}" in err


# -- execute (the in-cluster side) --------------------------------------------


def test_execute_parses_then_plans(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(EXECUTE)

    out = capsys.readouterr()
    assert f"run {RUN_ID}" in out.out
    assert "phase 2 (ingestion): definition parsed" in out.out
    assert "phase 3 (planning): 1 test, sequential" in out.out
    assert "pair-001  worker-01 -> worker-02" in out.out
    # Once to plan, once more in phase 4, to check the plan against the nodes as they
    # are when the tests are about to start, and once after the run, to see whether its
    # nodes are still Ready (phase 10).
    assert job_cluster.node_reads == 3
    assert "phase 4 (preflight): cluster claimed for this run until" in out.out
    # The faked test records no outcome, which is an error, never a pass.
    assert "phase 8 (validation): pair-001 error" in out.err
    assert code == ExitCode.ERROR


def test_execute_marks_its_run_finished_so_the_next_one_can_start(
    job_cluster: FakeCluster,
) -> None:
    # ADR-05-13: however a claimed run ends, it stops holding the cluster.
    main(EXECUTE)
    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION in owner["metadata"]["annotations"]

    submitted(job_cluster, "20260101-110000-aaaaaa")
    other = [*EXECUTE[:4], "20260101-110000-aaaaaa", *EXECUTE[5:]]
    assert main(other) == ExitCode.ERROR


def test_execute_dates_its_claim_from_the_jobs_start(job_cluster: FakeCluster) -> None:
    # ADR-05-13: the Job's start time plus its activeDeadlineSeconds, not the moment the
    # claim was written after phases 2 and 3.
    main(EXECUTE)

    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    expected = JOB_STARTED + timedelta(seconds=3600)
    assert owner["metadata"]["annotations"][CLAIM_EXPIRES_AT_ANNOTATION] == (
        expected.isoformat().replace("+00:00", "Z")
    )


def test_execute_is_refused_with_4_while_another_run_is_active(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    # ADR-01-11: a refused claim is a run-level preflight failure, reason named.
    submitted(job_cluster, "20260101-090000-bbbbbb")
    claim_run(
        job_cluster.store,
        RunId("20260101-090000-bbbbbb"),
        namespace="dokimi-iperf2",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    code = main(EXECUTE)

    assert code == ExitCode.PREFLIGHT_FAILURE
    err = capsys.readouterr().err
    assert "run_already_active" in err
    assert "20260101-090000-bbbbbb already active" in err
    assert "Traceback" not in err


def test_execute_provisions_and_names_its_prometheus_before_any_workload(
    job_cluster: FakeCluster, prometheus: FakePrometheus, capsys: pytest.CaptureFixture[str]
) -> None:
    # ADR-03-02: the selected mode and the resolved endpoint are logged prominently, and
    # query access is verified in phase 4.
    main(EXECUTE)

    out = capsys.readouterr().out
    endpoint = f"http://run-{RUN_ID}-prometheus.dokimi-iperf2.svc:9090"
    assert "monitoring mode managed (requested)" in out
    assert f"Prometheus  {endpoint}" in out
    assert "phase 4 (preflight): Prometheus answers queries" in out
    assert "kept after the run" in out
    assert any(url.startswith(f"{endpoint}/api/v1/query") for url in prometheus.urls)
    assert job_cluster.store.exists(DEPLOYMENT, "dokimi-iperf2", f"run-{RUN_ID}-prometheus")


def test_an_unqueryable_prometheus_fails_phase_4_before_any_workload(
    job_cluster: FakeCluster,
    prometheus: FakePrometheus,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The acceptance criterion: a cluster where Prometheus cannot be queried fails in
    # phase 4 (exit 4, "cluster not ready"), and no test workload is ever deployed.
    prometheus.answers = False
    monkeypatch.setattr(commands, "QUERY_ACCESS_TIMEOUT_SECONDS", 0)

    code = main(EXECUTE)

    assert code == ExitCode.PREFLIGHT_FAILURE
    err = capsys.readouterr().err
    assert "cluster not ready (prometheus_unreachable)" in err
    assert "connection refused" in err
    assert not [kind for kind, _ in job_cluster.store.created if kind is POD]
    # However phase 4 ends, the run stops holding the cluster (ADR-05-13).
    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION in owner["metadata"]["annotations"]


# -- phase 4's cluster checks (ADR-05-07) ----------------------------------------------


def test_execute_checks_the_cluster_with_the_first_pair_and_removes_it_before_phase_5(
    job_cluster: FakeCluster,
    probe: FakeProbe,
    robot: FakeRobotRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pods_at_phase_6: list[list[str]] = []
    robot.on_run = lambda: pods_at_phase_6.append(
        [pod["metadata"]["name"] for pod in job_cluster.store.list(POD, "", "dokimi-iperf2")]
    )

    main(EXECUTE)

    out = capsys.readouterr().out
    # The first pair, on its own nodes, with the client in probe mode.
    (spec,) = probe.specs
    assert spec.test_id == "pair-001-preflight"
    client = spec.pods[Role.CLIENT]["spec"]["containers"][0]
    assert client["args"][-2:] == ["--probe-dns", "kubernetes.default.svc"]
    assert len(probe.started) == 1
    assert "phase 4 (preflight): nodes worker-01, worker-02 accept test pods" in out
    assert "phase 4 (preflight): pair-001 scheduled, pulled its image and connected" in out
    assert "phase 4 (preflight): removed 2 test pod(s)" in out
    assert "phase 4 (preflight): the cluster is ready" in out
    assert out.index("the cluster is ready") < out.index("phase 5 (suite rendering)")
    # Phase 6 starts with the probe gone: its pods are never next to a test's.
    assert not [name for name in pods_at_phase_6[0] if "preflight" in name]


def test_a_failed_cluster_check_exits_4_and_runs_nothing(
    job_cluster: FakeCluster,
    probe: FakeProbe,
    robot: FakeRobotRunner,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The acceptance criteria: its own named error, no test workload left running, and
    # no test results.
    probe.result = ProbeResult(ProbeCheck.DNS, "kubernetes.default.svc did not resolve")

    code = main(EXECUTE)

    assert code == ExitCode.PREFLIGHT_FAILURE
    err = capsys.readouterr().err
    assert "cluster not ready (dns): the probe of pair-001 (worker-01 -> worker-02)" in err
    assert "Traceback" not in err
    assert robot.runs == []
    # No test results: phase 10's snapshot is all the run writes. It records the probe's
    # pods, which were still there, without making the pair look started or an anomaly.
    run_artifacts = tmp_path / "artifacts" / RUN_ID
    assert sorted(path.name for path in run_artifacts.iterdir()) == ["diagnostics.json"]
    report = json.loads((run_artifacts / "diagnostics.json").read_text())
    assert report["pairs"][0]["started"] is False
    assert {pod["test_id"] for pod in report["pods"]} == {"pair-001-preflight"}
    assert report["anomalies"] == []
    assert job_cluster.store.list(POD, f"dokimi.io/run-id={RUN_ID}", "dokimi-iperf2") == []
    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION in owner["metadata"]["annotations"]


def test_a_node_lost_since_planning_is_node_count(
    job_cluster: FakeCluster, probe: FakeProbe, capsys: pytest.CaptureFixture[str]
) -> None:
    # Phase 3 read both nodes; by phase 4 one of them is cordoned.
    reads = iter(
        [
            [node("worker-01"), node("worker-02")],
            [node("worker-01"), node("worker-02", cordoned=True)],
        ]
    )
    job_cluster.list_nodes = lambda: next(reads)  # type: ignore[method-assign]

    code = main(EXECUTE)

    assert code == ExitCode.PREFLIGHT_FAILURE
    err = capsys.readouterr().err
    assert "cluster not ready (node_count)" in err
    assert "worker-02 no longer accepts a test pod" in err
    assert probe.specs == []


def test_a_node_exporter_prometheus_cannot_scrape_is_a_warning_only(
    job_cluster: FakeCluster, prometheus: FakePrometheus, capsys: pytest.CaptureFixture[str]
) -> None:
    prometheus.targets = [
        {**SCRAPED, "health": "down", "lastError": "context deadline exceeded"},
    ]

    main(EXECUTE)

    captured = capsys.readouterr()
    assert (
        "node-exporter: Prometheus cannot scrape 1 of 1 node-exporter target(s): "
        "worker-01: context deadline exceeded" in captured.err
    )
    assert "phase 5 (suite rendering)" in captured.out


def test_execute_preflight_only_stops_after_phase_4_and_exits_0(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([*EXECUTE, "--preflight-only"])

    out = capsys.readouterr().out
    assert code == ExitCode.PASS
    assert "phase 4 (preflight): the cluster is ready" in out
    assert "preflight only: the run stops before any test" in out
    assert "phase 5" not in out
    assert robot.runs == []
    assert job_cluster.store.list(POD, f"dokimi.io/run-id={RUN_ID}", "dokimi-iperf2") == []
    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION in owner["metadata"]["annotations"]


def test_a_preflight_only_run_takes_phase_10_too(job_cluster: FakeCluster, tmp_path: Path) -> None:
    # Phase 10 runs on every path. After a passed preflight the probe's pods are gone, so
    # there is nothing to find, and nothing in it counts as a pair that started.
    main([*EXECUTE, "--preflight-only"])

    report = json.loads((tmp_path / "artifacts" / RUN_ID / "diagnostics.json").read_text())
    assert report["pods"] == []
    assert report["pairs"][0]["started"] is False
    assert report["anomalies"] == []
    assert report["retain"] is False


def test_the_preflight_command_submits_a_run_that_stops_after_phase_4(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    # One implementation, two entry points: the same Job, told to stop.
    code = main(["preflight", str(EXAMPLE)])

    assert code == ExitCode.PASS
    assert cluster.created_kinds == ["ConfigMap", "ConfigMap", "Job"]
    job = cluster.created[-1]
    assert job["spec"]["template"]["spec"]["containers"][0]["args"][-1] == "--preflight-only"
    # A preflight writes no artifacts, and says nothing about their absence.
    assert "carried no artifacts" not in capsys.readouterr().err


def test_the_preflight_command_takes_the_cluster_name(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A preflight is a run, so it needs the cluster's name like any run, and without the
    # variable it must offer the flag its refusal points to.
    monkeypatch.delenv("DOKIMI_CLUSTER")

    code = main(["preflight", str(EXAMPLE), "--cluster", "lab-fra-1"])

    assert code == ExitCode.PASS
    env = cluster.created[-1]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "DOKIMI_CLUSTER", "value": "lab-fra-1"} in env


def test_a_preflight_without_a_cluster_name_creates_nothing(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DOKIMI_CLUSTER")

    assert main(["preflight", str(EXAMPLE)]) == ExitCode.CONFIG_ERROR
    assert "--cluster <name> or DOKIMI_CLUSTER" in capsys.readouterr().err
    assert cluster.created == []


def test_a_submitted_run_does_not_stop_after_preflight(cluster: FakeCluster) -> None:
    main(["submit", str(EXAMPLE)])

    job = cluster.created[-1]
    assert "--preflight-only" not in job["spec"]["template"]["spec"]["containers"][0]["args"]


def test_existing_monitoring_is_refused_before_the_cluster_is_touched(
    no_cluster: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = tmp_path / "existing.yaml"
    definition.write_text(
        EXAMPLE.read_text().replace("mode: managed", "mode: existing"), encoding="utf-8"
    )
    arguments = [*EXECUTE]
    arguments[2] = str(definition)

    assert main(arguments) == ExitCode.CONFIG_ERROR
    assert "'existing' is not available" in capsys.readouterr().err


def existing_definition(tmp_path: Path) -> Path:
    definition = tmp_path / "existing.yaml"
    definition.write_text(
        EXAMPLE.read_text().replace("mode: managed", "mode: existing"), encoding="utf-8"
    )
    return definition


def test_validate_refuses_existing_monitoring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(existing_definition(tmp_path))]) == ExitCode.CONFIG_ERROR
    assert "'existing' is not available" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--dry-run"], []])
def test_submit_refuses_existing_monitoring_before_anything_is_created(
    no_cluster: None, tmp_path: Path, extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The fixture's client raises on contact: the refusal comes before any cluster call,
    # so no run is created that could only fail in phase 2.
    arguments = ["submit", str(existing_definition(tmp_path)), *extra]

    assert main(arguments) == ExitCode.CONFIG_ERROR
    assert "'existing' is not available" in capsys.readouterr().err


def test_execute_deploys_a_node_exporter_when_the_cluster_has_none(
    job_cluster: FakeCluster, prometheus: FakePrometheus, capsys: pytest.CaptureFixture[str]
) -> None:
    main(EXECUTE)

    out = capsys.readouterr().out
    name = f"run-{RUN_ID}-node-exporter"
    assert (DAEMON_SET, name) in [
        (kind, manifest["metadata"]["name"]) for kind, manifest in job_cluster.store.created
    ]
    assert f"node_exporter       deployed by this run (dokimi-iperf2/{name})" in out
    assert "node-exporter pods  pods not reported yet" in out
    # The 2026-10-07 decision: removed at the end of the run that deployed it.
    assert not job_cluster.store.exists(DAEMON_SET, "dokimi-iperf2", name)
    assert f"phase 11 (cleanup): removed node-exporter {name}" in out


def test_execute_deploys_the_monitoring_images_submit_chose(
    job_cluster: FakeCluster, prometheus: FakePrometheus
) -> None:
    main(EXECUTE)

    images = {
        manifest["metadata"]["name"]: container["image"]
        for kind, manifest in job_cluster.store.created
        if kind in (DAEMON_SET, DEPLOYMENT)
        for container in manifest["spec"]["template"]["spec"]["containers"]
    }
    assert images == {
        f"run-{RUN_ID}-node-exporter": f"ghcr.io/dokimi-io/node-exporter@{DIGEST}",
        f"run-{RUN_ID}-prometheus": f"ghcr.io/dokimi-io/prometheus@{DIGEST}",
    }


def test_execute_says_how_to_reach_its_kept_prometheus(
    job_cluster: FakeCluster, prometheus: FakePrometheus, capsys: pytest.CaptureFixture[str]
) -> None:
    main(EXECUTE)

    assert (
        f"query it from your machine with: dokimi prometheus {RUN_ID} --namespace dokimi-iperf2"
    ) in capsys.readouterr().out


def test_execute_keeps_its_prometheus_off_the_tested_nodes_where_it_can(
    job_cluster: FakeCluster, prometheus: FakePrometheus
) -> None:
    main(EXECUTE)

    deployment = job_cluster.store.get(DEPLOYMENT, "dokimi-iperf2", f"run-{RUN_ID}-prometheus")
    affinity = deployment["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]
    terms = affinity["preferredDuringSchedulingIgnoredDuringExecution"]
    assert [term["preference"]["matchFields"][0]["values"] for term in terms] == [
        ["worker-01"],
        ["worker-02"],
    ]


def test_execute_rejects_a_missing_node_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    given_images(monkeypatch)
    fake = FakeCluster(nodes=[node("worker-01"), node("worker-03")])
    monkeypatch.setattr(commands, "connect", lambda **_k: fake)

    code = main(EXECUTE)

    assert code == ExitCode.CONFIG_ERROR
    assert "target node 'worker-02' does not exist" in capsys.readouterr().err
    # Resolution happens before phase 6 deploys anything, so there is nothing to undo.
    assert fake.created == []


def test_execute_rejects_an_unschedulable_node_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    given_images(monkeypatch)
    fake = FakeCluster(nodes=[node("worker-01"), node("worker-02", cordoned=True)])
    monkeypatch.setattr(commands, "connect", lambda **_k: fake)

    code = main(EXECUTE)

    assert code == ExitCode.CONFIG_ERROR
    assert "cordoned" in capsys.readouterr().err


# -- the plan in a dry run ----------------------------------------------------


def test_a_plain_dry_run_says_the_nodes_were_not_checked(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.PASS

    err = capsys.readouterr().err
    assert "nodes not checked" in err
    assert "Nodes were not checked against a cluster" in err


def test_a_dry_run_with_a_kubeconfig_shows_the_resolved_plan(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["submit", str(EXAMPLE), "--dry-run", "--kubeconfig", "/some/kubeconfig"])

    err = capsys.readouterr().err
    assert code == ExitCode.PASS
    assert "Planned matrix (1 test, sequential)" in err
    assert "pair-001  worker-01 -> worker-02" in err
    assert "Both nodes exist and can accept a test pod" in err


def test_a_dry_run_with_a_kubeconfig_only_reads(cluster: FakeCluster) -> None:
    # ADR-01-06 allows a provided kubeconfig "for read-only cluster info". Read-only
    # means the fake records not one write.
    main(["submit", str(EXAMPLE), "--dry-run", "--context", "kind-test"])

    assert cluster.node_reads == 1
    assert cluster.created == []


def test_a_dry_run_with_a_kubeconfig_catches_a_missing_node(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeCluster(nodes=[node("worker-01")])
    monkeypatch.setattr(commands, "connect", lambda **_k: fake)

    code = main(["submit", str(EXAMPLE), "--dry-run", "--context", "kind-test"])

    assert code == ExitCode.CONFIG_ERROR
    assert "target node 'worker-02' does not exist" in capsys.readouterr().err


# -- image and top level ------------------------------------------------------


def test_image_prints_the_released_default(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["image"]) == ExitCode.PASS
    out = capsys.readouterr().out
    assert IMAGE in out
    assert "source: release-default" in out


def test_image_honours_the_run_override(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["image", "--orchestrator-image", "registry.internal/orchestrator:1.2.3"]) == 0
    out = capsys.readouterr().out
    assert "registry.internal/orchestrator:1.2.3" in out
    assert "source: run-override" in out


def test_image_honours_the_installation_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOKIMI_ORCHESTRATOR_IMAGE", f"registry.internal/orchestrator@{DIGEST}")

    assert main(["image"]) == ExitCode.PASS
    assert "source: installation-override" in capsys.readouterr().out


def test_image_honours_the_component_override(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["image", "--image", "orchestrator=registry.internal/orchestrator:1.2.3"]) == 0
    assert "registry.internal/orchestrator:1.2.3" in capsys.readouterr().out


def test_image_refuses_two_names_for_different_orchestrator_images() -> None:
    code = main(
        [
            "image",
            "--orchestrator-image",
            "registry.internal/orchestrator:1.2.3",
            "--image",
            "orchestrator=registry.internal/orchestrator:1.2.4",
        ]
    )

    assert code == ExitCode.CONFIG_ERROR


def test_image_on_a_development_build_lists_every_component_anyway(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A diagnostic: it says what is missing for each component, and submit refuses.
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", None)
    monkeypatch.setattr(_image_digest, "IPERF2_IMAGE_DIGEST", None)

    assert main(["image"]) == ExitCode.PASS
    out = capsys.readouterr().out
    # The monitoring images are pinned upstream digests, so a source build has them too.
    assert out.count("no default in a development build") == 2
    assert "prometheus     quay.io/prometheus/prometheus:" in out
    assert "--image iperf2=<reference> or DOKIMI_IPERF2_IMAGE" in out


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == ExitCode.PASS
    assert "usage: dokimi" in capsys.readouterr().out


def test_version_reports_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("dokimi ")


def test_an_unknown_command_is_a_config_error() -> None:
    # argparse would exit 2, which ADR-01-08 reserves for `error`: a typo on the command
    # line would read to CI as a run that errored. It is an invalid request: 3.
    with pytest.raises(SystemExit) as exit_info:
        main(["teleport"])

    assert exit_info.value.code == ExitCode.CONFIG_ERROR


def test_an_unknown_flag_is_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["submit", str(EXAMPLE), "--dry-rn"])

    assert exit_info.value.code == ExitCode.CONFIG_ERROR
    assert "unrecognized arguments: --dry-rn" in capsys.readouterr().err


def test_help_still_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["submit", "--help"])

    assert exit_info.value.code == 0


def test_a_bad_run_id_on_submit_is_a_config_error(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # Previously only `attach` validated it; `submit` sent it to the API server.
    assert (
        main(["submit", str(EXAMPLE), "--run-id", "My Run", "--dry-run"]) == ExitCode.CONFIG_ERROR
    )
    assert "malformed run id" in capsys.readouterr().err


def test_an_unexpected_exception_is_internal_not_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Python exits 1 on an unhandled exception, and 1 is `fail`: CI would read a crash as
    # a run that measured something and did not pass.
    def crash(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster())
    monkeypatch.setattr(commands, "submit", crash)

    assert main(["submit", str(EXAMPLE)]) == ExitCode.INTERNAL
    err = capsys.readouterr().err
    assert "internal error (RuntimeError: something nobody anticipated)" in err
    assert "Traceback" in err


def test_an_interrupt_is_still_not_swallowed_by_the_catch_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # KeyboardInterrupt is not an Exception, so Ctrl-C outside the log stream still stops
    # the CLI rather than being reported as an internal error.
    def interrupt(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster())
    monkeypatch.setattr(commands, "submit", interrupt)

    with pytest.raises(KeyboardInterrupt):
        main(["submit", str(EXAMPLE)])


def test_a_cluster_error_carries_its_notes_to_the_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "connect", lambda **_k: FakeCluster(fail_on_kind="Job"))

    assert main(["submit", str(EXAMPLE)]) == ExitCode.INTERNAL
    assert "422" in capsys.readouterr().err


def test_the_in_cluster_command_is_not_advertised(capsys: pytest.CaptureFixture[str]) -> None:
    # `execute` is the Job's entry point, not something a person runs. It still works
    # when named, but listing it invites someone to run a phase-2-only orchestrator by
    # hand and wonder why it exits 5.
    with pytest.raises(SystemExit):
        main(["--help"])

    listed = capsys.readouterr().out
    commands = [line.strip().split()[0] for line in listed.splitlines() if line.startswith("    ")]
    assert "validate" in commands
    assert "execute" not in commands
    assert "SUPPRESS" not in listed


# -- the images a run deploys (ADR-04-08) ---------------------------------------------


def test_a_dry_run_shows_the_images_the_run_deploys(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--dry-run", "--image", "iperf2=iperf2:dev"]) == 0
    captured = capsys.readouterr()

    assert "  iperf2         iperf2:dev (run-override)" in captured.err
    assert f"  orchestrator   {IMAGE} (release-default)" in captured.err
    assert "  node-exporter  quay.io/prometheus/node-exporter:" in captured.err
    # They are part of the rendered Job, which is what submit would create.
    assert "name: DOKIMI_IPERF2_IMAGE\n" in captured.out
    assert "value: iperf2:dev\n" in captured.out
    assert "value: run-override\n" in captured.out


def test_the_installation_override_reaches_the_job_resolved(
    no_cluster: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOKIMI_IPERF2_IMAGE", "mirror.local/iperf2:2.2.1")

    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.PASS
    out = capsys.readouterr().out

    assert "value: mirror.local/iperf2:2.2.1\n" in out
    assert "value: installation-override\n" in out


def test_a_source_build_without_an_iperf2_image_creates_nothing(
    no_cluster: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_image_digest, "IPERF2_IMAGE_DIGEST", None)

    assert main(["submit", str(EXAMPLE)]) == ExitCode.CONFIG_ERROR
    assert "--image iperf2=<reference> or DOKIMI_IPERF2_IMAGE" in capsys.readouterr().err


def test_an_unknown_image_component_is_a_config_error(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--image", "iperf3=x:1"]) == ExitCode.CONFIG_ERROR
    assert "no component 'iperf3'" in capsys.readouterr().err


def test_image_lists_every_image_and_where_it_came_from(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["image", "--image", "iperf2=iperf2:dev"]) == ExitCode.PASS
    out = capsys.readouterr().out

    assert f"orchestrator   {IMAGE}" in out
    assert "iperf2         iperf2:dev\n  source: run-override\n" in out
    assert "node-exporter  quay.io/prometheus/node-exporter:" in out


def test_execute_reports_the_images_it_was_given(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    main(EXECUTE)

    assert f"image       iperf2 ghcr.io/dokimi-io/iperf2@{DIGEST} (release-default)" in (
        capsys.readouterr().out
    )


def test_execute_without_its_images_is_refused_before_it_claims(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    # A Job this CLI did not make. Nothing is claimed, so nothing has to be released.
    submitted(cluster, RUN_ID)

    assert main(EXECUTE) == ExitCode.CONFIG_ERROR
    assert "submitted without an image for orchestrator" in capsys.readouterr().err
    owner = cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert CLAIM_EXPIRES_AT_ANNOTATION not in (owner["metadata"].get("annotations") or {})


# -- the cluster's name (ADR-02-07) ------------------------------------------------------


def test_submit_without_a_cluster_name_creates_nothing(
    no_cluster: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No default: refused before anything is rendered, a dry run included.
    monkeypatch.delenv("DOKIMI_CLUSTER")

    assert main(["submit", str(EXAMPLE), "--dry-run"]) == ExitCode.CONFIG_ERROR
    assert "--cluster <name> or DOKIMI_CLUSTER" in capsys.readouterr().err


def test_a_malformed_cluster_name_is_a_config_error(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--dry-run", "--cluster", "Lab EU"]) == (
        ExitCode.CONFIG_ERROR
    )
    assert "--cluster 'Lab EU' is not a cluster name" in capsys.readouterr().err


def test_the_cluster_flag_reaches_the_job_and_the_dry_run(
    no_cluster: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["submit", str(EXAMPLE), "--dry-run", "--cluster", "lab-fra-1"]) == 0
    captured = capsys.readouterr()

    assert "Cluster    lab-fra-1" in captured.err
    assert "name: DOKIMI_CLUSTER\n" in captured.out
    assert "value: lab-fra-1\n" in captured.out
    assert "value: test-cluster\n" not in captured.out


def test_execute_reports_the_cluster_it_was_given(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    main(EXECUTE)

    assert "  cluster     test-cluster" in capsys.readouterr().out


def test_execute_without_a_cluster_name_is_refused_before_it_claims(
    job_cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DOKIMI_CLUSTER")

    assert main(EXECUTE) == ExitCode.CONFIG_ERROR
    assert "submitted without a cluster name" in capsys.readouterr().err
    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert CLAIM_EXPIRES_AT_ANNOTATION not in (owner["metadata"].get("annotations") or {})


# -- phases 5, 6 and 9a (M1-11) -------------------------------------------------------


def test_execute_renders_runs_one_robot_process_per_pair_and_merges(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    main(EXECUTE)
    out = capsys.readouterr().out

    assert "phase 5 (suite rendering):" in out
    assert "phase 6 (execution): pair-001  worker-01 -> worker-02" in out
    assert "phase 9a (reporting):" in out
    assert len(robot.runs) == 1
    assert robot.merges == [[robot.runs[0].output]]


def test_each_robot_process_is_told_its_run_and_its_images(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    main(EXECUTE)
    environment = robot.runs[0].environment

    assert environment["DOKIMI_RUN_ID"] == RUN_ID
    assert environment["DOKIMI_NAMESPACE"] == "dokimi-iperf2"
    assert environment["DOKIMI_TEST_ID"] == "pair-001"
    assert environment["DOKIMI_IPERF2_IMAGE"] == f"ghcr.io/dokimi-io/iperf2@{DIGEST}"


def test_the_suite_that_runs_is_what_convert_prints(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    # M1-11's second acceptance criterion: one renderer, called from two places.
    main(EXECUTE)
    executed = robot.runs[0].suite.read_bytes()
    capsys.readouterr()

    assert main(["convert", str(EXAMPLE)]) == ExitCode.PASS
    assert capsys.readouterr().out.encode("utf-8") == executed


def test_the_rendered_suite_is_kept_under_the_run_id(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    main(EXECUTE)

    assert robot.runs[0].suite.parent.name == RUN_ID
    assert robot.runs[0].suite.name == "suite.robot"


def test_a_robot_process_that_wrote_nothing_is_an_internal_error(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    robot.writes_output = False

    assert main(EXECUTE) == ExitCode.INTERNAL
    assert "wrote no pair-001.xml" in capsys.readouterr().err
    # The run still releases the cluster on its way out.
    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION in owner["metadata"]["annotations"]


def test_convert_renders_yaml_too(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["convert", str(EXAMPLE), "--to", "yaml"]) == ExitCode.PASS
    assert "kind: NetworkTest" in capsys.readouterr().out


def test_convert_touches_no_cluster(no_cluster: None) -> None:
    assert main(["convert", str(EXAMPLE)]) == ExitCode.PASS


# -- the run's artifacts, back through its log (ADR-05-15, provisional) ---------------


def artifact_lines(tmp_path: Path, run_id: str) -> list[str]:
    """What a finished Job's log ends with: its artifact directory, as one block."""
    from dokimi.artifact_stream import write_artifacts  # noqa: PLC0415

    directory = tmp_path / "job" / run_id
    directory.mkdir(parents=True)
    (directory / "log.html").write_text("<html>the run's log</html>", encoding="utf-8")
    lines: list[str] = []
    write_artifacts(directory, run_id, lines.append)
    return lines


def test_execute_ends_its_log_with_its_artifacts(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    main(EXECUTE)
    out = capsys.readouterr().out.splitlines()

    begins = [line for line in out if line.startswith("::dokimi-artifacts begin")]
    assert len(begins) == 1
    assert out[-1] == f"::dokimi-artifacts end run={RUN_ID}::"


def test_submit_extracts_the_artifacts_into_the_documents_results_directory(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster.log_lines = ["run started", *artifact_lines(tmp_path, RUN_ID), "run finished"]
    monkeypatch.chdir(tmp_path)

    main(["submit", str(EXAMPLE), "--run-id", RUN_ID])
    captured = capsys.readouterr()

    # The example says results.directory: ./results.
    assert (tmp_path / "results" / RUN_ID / "log.html").read_text() == "<html>the run's log</html>"
    assert "::dokimi-artifacts" not in captured.out
    assert captured.out.splitlines() == ["run started", "run finished"]
    assert f"artifacts: results/{RUN_ID}" in captured.err


def test_attach_finds_the_results_directory_in_the_runs_own_definition(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = EXAMPLE.read_text(encoding="utf-8").replace("./results", "./elsewhere")
    cluster.store.put(
        CONFIG_MAP,
        "dokimi-iperf2",
        {
            "metadata": {"name": resource_name(RUN_ID, "-definition")},
            "data": {"definition.yaml": source},
        },
    )
    cluster.log_lines = artifact_lines(tmp_path, RUN_ID)
    monkeypatch.chdir(tmp_path)

    main(["attach", RUN_ID])

    assert (tmp_path / "elsewhere" / RUN_ID / "log.html").is_file()


def test_a_damaged_block_is_reported_and_does_not_change_the_verdict(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = artifact_lines(tmp_path, RUN_ID)
    lines[1] = "!" * len(lines[1])
    cluster.log_lines = lines
    monkeypatch.chdir(tmp_path)

    code = main(["submit", str(EXAMPLE), "--run-id", RUN_ID])

    assert code == ExitCode.PASS
    assert "artifacts were not extracted" in capsys.readouterr().err
    assert not (tmp_path / "results").exists()


def test_an_unreadable_archive_is_reported_and_does_not_change_the_verdict(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A good checksum over a bad archive used to escape as a traceback, replacing the
    # run's exit code.
    import base64  # noqa: PLC0415
    import hashlib  # noqa: PLC0415

    data = b"\x1f\x8bnot a gzip stream"
    cluster.log_lines = [
        f"::dokimi-artifacts begin v=1 run={RUN_ID} bytes={len(data)} "
        f"sha256={hashlib.sha256(data).hexdigest()}::",
        base64.b64encode(data).decode(),
        f"::dokimi-artifacts end run={RUN_ID}::",
    ]
    monkeypatch.chdir(tmp_path)

    code = main(["submit", str(EXAMPLE), "--run-id", RUN_ID])

    assert code == ExitCode.PASS
    assert "the artifact archive cannot be read" in capsys.readouterr().err


def test_a_log_without_artifacts_says_so(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster.log_lines = ["run started", "run finished"]
    monkeypatch.chdir(tmp_path)

    assert main(["submit", str(EXAMPLE), "--run-id", RUN_ID]) == ExitCode.PASS
    assert "the run's log carried no artifacts" in capsys.readouterr().err


def test_attach_can_be_told_where_the_artifacts_go(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without reading the run's definition, so without get on its ConfigMaps.
    cluster.log_lines = artifact_lines(tmp_path, RUN_ID)
    monkeypatch.chdir(tmp_path)

    main(["attach", RUN_ID, "--results-directory", str(tmp_path / "chosen")])

    assert (tmp_path / "chosen" / RUN_ID / "log.html").is_file()


# -- once the artifacts are here, the run's Job goes ---------------------------------------


def test_a_run_that_kept_nothing_is_removed_once_its_artifacts_are_fetched(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster.log_lines = artifact_lines(tmp_path, RUN_ID)
    monkeypatch.chdir(tmp_path)

    assert main(["submit", str(EXAMPLE), "--run-id", RUN_ID]) == ExitCode.PASS

    # Its owner goes, and the garbage collector takes the Job and the definition with it.
    assert not cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert f"removed run {RUN_ID} from the cluster" in capsys.readouterr().err


def test_a_run_that_kept_its_evidence_loses_only_its_job_once_fetched(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner = owner_manifest(RunId(RUN_ID), "dokimi-iperf2")
    owner["metadata"]["annotations"] = {
        CLAIMED_AT_ANNOTATION: "2026-01-01T10:00:00Z",
        FINISHED_AT_ANNOTATION: "2026-01-01T10:05:00Z",
        RETAINED_ANNOTATION: "fail",
    }
    cluster.store.put(CONFIG_MAP, "dokimi-iperf2", owner)
    cluster.store.put(
        JOB,
        "dokimi-iperf2",
        {
            "metadata": {
                "name": resource_name(RUN_ID),
                "labels": ownership_labels(RunId(RUN_ID), component="orchestrator"),
            }
        },
    )
    cluster.log_lines = artifact_lines(tmp_path, RUN_ID)
    monkeypatch.chdir(tmp_path)

    main(["attach", RUN_ID, "--results-directory", str(tmp_path / "chosen")])

    assert cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert not cluster.store.exists(JOB, "dokimi-iperf2", resource_name(RUN_ID))
    assert f"What the run kept stays until: dokimi cleanup {RUN_ID}" in capsys.readouterr().err


def test_a_run_whose_artifacts_did_not_arrive_keeps_its_job_to_reattach_to(
    cluster: FakeCluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = artifact_lines(tmp_path, RUN_ID)
    lines[1] = "!" * len(lines[1])
    cluster.log_lines = lines
    monkeypatch.chdir(tmp_path)

    main(["submit", str(EXAMPLE), "--run-id", RUN_ID])

    assert cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert cluster.store.exists(JOB, "dokimi-iperf2", resource_name(RUN_ID))


def test_a_job_that_cant_be_removed_is_left_to_its_hour(
    cluster: FakeCluster,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cluster.log_lines = artifact_lines(tmp_path, RUN_ID)
    cluster.store.fail_deleting = True
    monkeypatch.chdir(tmp_path)

    # The verdict is the run's, whatever happens to its Job.
    assert main(["submit", str(EXAMPLE), "--run-id", RUN_ID]) == ExitCode.PASS
    assert "The cluster removes it an hour after the run ended" in capsys.readouterr().err


# -- dokimi cleanup -----------------------------------------------------------------------


def run_objects(fake: FakeCluster, run_id: str = RUN_ID) -> None:
    """What a finished run left: its owner, a test pod, and its kept Prometheus."""
    fake.store.put(CONFIG_MAP, "dokimi-iperf2", owner_manifest(RunId(run_id), "dokimi-iperf2"))
    for kind, name, component in (
        (POD, f"run-{run_id}-pair-001-client", "client"),
        (DEPLOYMENT, f"run-{run_id}-prometheus", "prometheus"),
    ):
        fake.store.put(
            kind,
            "dokimi-iperf2",
            {
                "metadata": {
                    "name": name,
                    "labels": ownership_labels(RunId(run_id), component=component),
                }
            },
        )


def test_cleanup_removes_a_run_and_keeps_its_prometheus(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    run_objects(cluster)

    assert main(["cleanup", RUN_ID]) == ExitCode.PASS

    out = capsys.readouterr().out
    assert f"run {RUN_ID}: removed 2 object(s)" in out
    assert f"  Pod/run-{RUN_ID}-pair-001-client" in out
    assert "Add --prometheus to remove it too" in out
    assert cluster.store.exists(DEPLOYMENT, "dokimi-iperf2", f"run-{RUN_ID}-prometheus")


def test_cleanup_with_prometheus_removes_it_too(cluster: FakeCluster) -> None:
    run_objects(cluster)

    main(["cleanup", RUN_ID, "--prometheus"])

    assert not cluster.store.exists(DEPLOYMENT, "dokimi-iperf2", f"run-{RUN_ID}-prometheus")


def test_cleanup_refuses_a_run_that_still_holds_the_cluster(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    run_objects(cluster)
    owner = cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    owner["metadata"]["annotations"] = {
        CLAIMED_AT_ANNOTATION: "2026-01-01T10:00:00Z",
        CLAIM_EXPIRES_AT_ANNOTATION: "2999-01-01T00:00:00Z",
    }
    cluster.store.replace(CONFIG_MAP, "dokimi-iperf2", owner)

    assert main(["cleanup", RUN_ID]) == ExitCode.PREFLIGHT_FAILURE
    assert "still holds the cluster" in capsys.readouterr().err
    assert main(["cleanup", RUN_ID, "--force"]) == ExitCode.PASS


def test_cleanup_that_cant_remove_something_says_how_to_retry(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    run_objects(cluster)
    cluster.store.fail_deleting = True

    assert main(["cleanup", RUN_ID]) == ExitCode.INTERNAL
    err = capsys.readouterr().err
    assert f"could not delete run {RUN_ID}'s test pods" in err
    assert "the rest of the run stays until they go" in err
    assert f"retry with: dokimi cleanup {RUN_ID} --namespace dokimi-iperf2" in err
    # Its owner, and so a forced run's claim, stays while a test pod may still be sending.
    assert cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))


def test_the_retry_line_repeats_the_flags_given(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    run_objects(cluster)
    cluster.store.fail_deleting = True

    main(["cleanup", RUN_ID, "--prometheus", "--force"])

    assert (
        f"retry with: dokimi cleanup {RUN_ID} --namespace dokimi-iperf2 --prometheus --force"
        in capsys.readouterr().err
    )


def holding(fake: FakeCluster, run_id: str) -> None:
    """Mark *run_id*'s owner as holding the cluster for the next half hour."""
    owner = fake.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(run_id)))
    expires = datetime.now(UTC) + timedelta(minutes=30)
    owner["metadata"]["annotations"] = {
        CLAIMED_AT_ANNOTATION: "2026-01-01T10:00:00Z",
        CLAIM_EXPIRES_AT_ANNOTATION: expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    fake.store.replace(CONFIG_MAP, "dokimi-iperf2", owner)


def test_cleanup_by_age_skips_a_run_that_holds_the_cluster_and_counts_it(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    run_objects(cluster)
    holding(cluster, RUN_ID)

    assert main(["cleanup", "--older-than", "24h"]) == ExitCode.PASS

    captured = capsys.readouterr()
    assert "still holds the cluster" in captured.err
    assert "skipped 1 of 1 run(s), which can't be removed now" in captured.out
    assert cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))


def test_cleanup_by_age_goes_on_after_a_run_it_could_not_reach(
    cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = "20251231-100000-bbbbbb"
    run_objects(cluster)
    run_objects(cluster, other)
    from dokimi.cleanup import clean_up_run as real  # noqa: PLC0415

    def times_out_on_the_first(resources: Any, run_id: RunId, *args: Any, **kwargs: Any) -> Any:
        if str(run_id) == other:
            raise ClusterError("reading ConfigMap: timed out")
        return real(resources, run_id, *args, **kwargs)

    monkeypatch.setattr(commands, "clean_up_run", times_out_on_the_first)

    assert main(["cleanup", "--older-than", "24h"]) == ExitCode.INTERNAL

    assert f"run {other}: could not remove it" in capsys.readouterr().err
    assert not cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))


def test_cleanup_dry_run_removes_nothing(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    run_objects(cluster)

    assert main(["cleanup", RUN_ID, "--dry-run"]) == ExitCode.PASS

    assert f"run {RUN_ID}: would remove 2 object(s)" in capsys.readouterr().out
    assert cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))


def test_cleanup_by_age_removes_only_older_runs(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    recent = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-bbbbbb"
    run_objects(cluster)
    run_objects(cluster, recent)

    assert main(["cleanup", "--older-than", "24h"]) == ExitCode.PASS

    assert not cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(recent)))
    assert f"run {recent}" not in capsys.readouterr().out


def test_cleanup_by_age_with_nothing_that_old_says_so(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["cleanup", "--older-than", "1h"]) == ExitCode.PASS
    assert "no run in dokimi-iperf2 started more than 1h ago" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments", [[], [RUN_ID, "--older-than", "24h"]], ids=["neither", "both"]
)
def test_cleanup_takes_a_run_id_or_an_age(no_cluster: None, arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["cleanup", *arguments])
    assert exited.value.code == ExitCode.CONFIG_ERROR


@pytest.mark.parametrize(
    "arguments",
    [
        ["--older-than", "24h", "--force"],
        ["--older-than", "a day"],
        ["--older-than", "30000000h"],
        ["not-a-run-id"],
    ],
    ids=["force by age", "malformed age", "age before any run", "malformed run id"],
)
def test_a_cleanup_it_cant_carry_out_is_a_config_error(
    no_cluster: None, arguments: list[str]
) -> None:
    assert main(["cleanup", *arguments]) == ExitCode.CONFIG_ERROR


# -- phase 11 starts: the run's test pods go before the claim does (ADR-05-13) ---------


def left_pod(fake: FakeCluster, name: str, component: str = "client") -> None:
    """A pod phase 6 left behind, labelled like every run-owned object."""
    from dokimi.k8s.resources import POD  # noqa: PLC0415

    fake.store.put(
        POD,
        "dokimi-iperf2",
        {
            "metadata": {
                "name": name,
                "labels": ownership_labels(RunId(RUN_ID), component=component),
            }
        },
    )


MEASURED = {
    "dokimi_throughput_bits_per_second": 49983531.0,
    "dokimi_packet_loss_percent": 0.0,
}


def held_by_prometheus(values: dict[str, float]) -> list[dict[str, Any]]:
    labels = {"run_id": RUN_ID, "test_id": "pair-001", "direction": "inbound"}
    return [
        {"metric": {"__name__": name, **labels}, "value": [0, repr(value)]}
        for name, value in values.items()
    ]


def test_phase_7_confirms_prometheus_holds_what_the_direct_read_found(
    job_cluster: FakeCluster,
    robot: FakeRobotRunner,
    prometheus: FakePrometheus,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    robot.measures = MEASURED
    prometheus.series = held_by_prometheus(MEASURED)

    main(EXECUTE)

    out = capsys.readouterr().out
    assert "phase 7 (measurement): pair-001, read directly from its exporters (inbound)" in out
    assert "phase 7 (measurement): Prometheus holds the series of pair-001" in out
    report = json.loads((tmp_path / "artifacts" / RUN_ID / "measurement.json").read_text())
    assert report["scrape_interval_seconds"] == 5
    (test,) = report["tests"]
    assert test["source"] == "direct"
    assert test["history_captured"] is True
    assert test["prometheus"] == MEASURED


def test_a_blocked_scrape_warns_and_the_run_goes_on(
    job_cluster: FakeCluster,
    robot: FakeRobotRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Acceptance criterion: a deliberately blocked scrape is a warning, and the run
    # completes. Prometheus holds nothing of the test here.
    robot.measures = MEASURED
    monkeypatch.setattr(commands, "SCRAPE_CONFIRMATION_TIMEOUT_SECONDS", 0.0)

    code = main(EXECUTE)

    captured = capsys.readouterr()
    assert "HISTORY NOT CAPTURED for pair-001" in captured.err
    assert "The verdict comes from the direct read and stands" in captured.err
    # The run went on to its reports, as before this phase existed.
    assert "phase 9a (reporting)" in captured.out
    assert code == ExitCode.ERROR
    report = json.loads((tmp_path / "artifacts" / RUN_ID / "measurement.json").read_text())
    assert report["tests"][0]["history_captured"] is False


def test_a_value_prometheus_holds_differently_is_reported(
    job_cluster: FakeCluster,
    robot: FakeRobotRunner,
    prometheus: FakePrometheus,
    capsys: pytest.CaptureFixture[str],
) -> None:
    robot.measures = MEASURED
    prometheus.series = held_by_prometheus(
        {**MEASURED, "dokimi_throughput_bits_per_second": 40_000_000.0}
    )

    main(EXECUTE)

    err = capsys.readouterr().err
    assert "pair-001 dokimi_throughput_bits_per_second: the direct read found 49983531.0" in err
    assert "Prometheus holds 40000000.0" in err


def test_a_pair_without_a_measurement_is_named(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    # Its test ended before the direct read, by an error Robot's report names.
    main(EXECUTE)

    assert "pair-001 has no measurement" in capsys.readouterr().err


def test_phase_8_prints_each_tests_outcome_as_its_robot_process_decided_it(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = ExpectedPair("pair-001", "worker-01", "worker-02")
    measurement = Measurement("pair-001", "inbound", MEASURED, {})
    robot.validates = [
        evaluate(criterion, {"pair-001": measurement}, [pair])
        for criterion in parse_file(EXAMPLE).criteria
    ]

    code = main(EXECUTE)

    out = capsys.readouterr().out
    assert "phase 8 (validation): pair-001 fail, 2 criterion(s)" in out
    assert (
        "  fail  Throughput Should Be At Least 950 megabits per second: "
        "49.98353 megabits per second" in out
    )
    assert "  pass  Packet Loss Should Be At Most 0 percent: 0 percent" in out
    # The worst test outcome is the run's exit code.
    assert code == ExitCode.FAIL


def test_a_test_with_no_criteria_is_an_error_in_phase_8(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only a suite written by hand gets here: both formats refuse it.
    robot.validates = []

    main(EXECUTE)

    out = capsys.readouterr().out
    assert "phase 8 (validation): pair-001 error, 0 criterion(s)" in out
    assert "  error  nothing asserted: the test has no pass criterion" in out


def test_a_test_that_recorded_nothing_is_an_error(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    # Its Robot process ended before it wrote anything: no data is never a pass.
    assert main(EXECUTE) == ExitCode.ERROR

    assert (
        "phase 8 (validation): pair-001 error: it ended before its outcome was recorded"
        in capsys.readouterr().err
    )


def test_a_test_that_ended_before_its_criteria_says_why(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    robot.ends_with = TestError(ErrorCategory.IMAGE_PULL, "pair-001 could not pull its image")

    assert main(EXECUTE) == ExitCode.ERROR

    out = capsys.readouterr().out
    assert "phase 8 (validation): pair-001 error, 0 criterion(s)" in out
    assert "  error (image_pull)  ended before its criteria: pair-001 could not pull" in out


def test_a_run_whose_tests_all_pass_exits_0(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    recorded(robot, holds=True)

    assert main(EXECUTE) == ExitCode.PASS


def test_an_anomaly_makes_a_passing_run_an_error(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    # Something went wrong around the test: its result is not the whole picture.
    recorded(robot, holds=True)
    robot.on_run = lambda: server_lost(job_cluster)

    assert main(EXECUTE) == ExitCode.ERROR


def recorded(robot: FakeRobotRunner, *, holds: bool) -> None:
    """Make the faked test record one criterion that holds, or doesn't."""
    pair = ExpectedPair("pair-001", "worker-01", "worker-02")
    measurement = Measurement("pair-001", "inbound", MEASURED, {})
    threshold = Decimal(1 if holds else 950)
    throughput = Criterion(
        Metric.THROUGHPUT, Comparison.AT_LEAST, threshold, Unit.MEGABITS_PER_SECOND
    )
    robot.validates = [evaluate(throughput, {"pair-001": measurement}, [pair])]


def pair_pods(fake: FakeCluster) -> None:
    """The pair's two pods as phase 6 leaves them, labelled with their test.

    Called from the fake Robot process, as ``robot.on_run``: phase 4 removes every test pod
    of the run before phase 5, so pods put there before the run would be gone by phase 10.
    """
    from dokimi.k8s.resources import POD  # noqa: PLC0415

    for role, node_name in (("client", "worker-01"), ("server", "worker-02")):
        fake.store.put(
            POD,
            "dokimi-iperf2",
            {
                "metadata": {
                    "name": f"run-{RUN_ID}-pair-001-{role}",
                    "labels": ownership_labels(RunId(RUN_ID), test_id="pair-001", component=role),
                },
                "spec": {"nodeName": node_name},
                "status": {"phase": "Running"},
            },
        )


def server_lost(fake: FakeCluster) -> None:
    """The pair's pods as phase 6 leaves them, but its server pod has gone: an anomaly."""
    pair_pods(fake)
    fake.store.objects.pop((POD.kind, "dokimi-iperf2", f"run-{RUN_ID}-pair-001-server"))


def removed_pair_pods(fake: FakeCluster) -> list[str]:
    """The pair's pods deleted so far, without the preflight's probe pods phase 4 removes."""
    pods = {f"run-{RUN_ID}-pair-001-{role}" for role in ("client", "server")}
    return [name for kind, _, name in fake.store.deleted if kind == "Pod" and name in pods]


def test_phase_10_records_the_run_next_to_robots_reports(
    job_cluster: FakeCluster,
    robot: FakeRobotRunner,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Acceptance criterion: on every run, passing ones included.
    robot.on_run = lambda: pair_pods(job_cluster)

    main(EXECUTE)

    out = capsys.readouterr().out
    assert "phase 10 (diagnostics): 2 pod(s), 1 pair(s), none down" in out
    assert f"  run-{RUN_ID}-pair-001-server on worker-02: Running" in out
    report = json.loads((tmp_path / "artifacts" / RUN_ID / "diagnostics.json").read_text())
    assert report["retain"] is False
    assert report["pairs"][0]["findings"] == "pairs/pair-001.xml"
    # Labelled like every result, with the name given at submission.
    assert report["cluster"] == "test-cluster"


def test_phase_10_checks_the_runs_prometheus_is_still_up(
    job_cluster: FakeCluster, robot: FakeRobotRunner, prometheus: FakePrometheus, tmp_path: Path
) -> None:
    robot.on_run = lambda: pair_pods(job_cluster)

    main(EXECUTE)

    report = json.loads((tmp_path / "artifacts" / RUN_ID / "diagnostics.json").read_text())
    assert report["reachable"]["prometheus"] is True
    # Asked once in phase 4, and once more after the run.
    assert len([url for url in prometheus.urls if url.endswith("/-/ready")]) >= 2


def test_phase_10_is_taken_before_cleanup_removes_anything(
    job_cluster: FakeCluster, robot: FakeRobotRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Acceptance criterion: cleanup cannot start before the snapshot completes.
    robot.on_run = lambda: pair_pods(job_cluster)
    removed_when_captured: list[list[str]] = []
    from dokimi.diagnostics import capture as real  # noqa: PLC0415

    def removed_pods() -> list[str]:
        return removed_pair_pods(job_cluster)

    def spy(*args: Any, **kwargs: Any) -> Any:
        removed_when_captured.append(removed_pods())
        return real(*args, **kwargs)

    monkeypatch.setattr(commands, "capture", spy)

    main(EXECUTE)

    assert removed_when_captured == [[]]
    assert len(removed_pods()) == 2


def test_phase_10_is_taken_when_the_run_fails_too(
    job_cluster: FakeCluster, robot: FakeRobotRunner, tmp_path: Path
) -> None:
    robot.on_run = lambda: pair_pods(job_cluster)
    robot.writes_output = False  # a crashed Robot process

    assert main(EXECUTE) == ExitCode.INTERNAL
    assert (tmp_path / "artifacts" / RUN_ID / "diagnostics.json").is_file()


def test_an_anomaly_means_the_run_is_retained(
    job_cluster: FakeCluster,
    robot: FakeRobotRunner,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Only the client is left: the server pod of a pair that started has gone.
    robot.on_run = lambda: server_lost(job_cluster)

    main(EXECUTE)

    out = capsys.readouterr().out
    assert "  1 anomaly(ies): the run's resources are retained" in out
    assert (
        "    pod_missing pair-001/server: its pods were deployed, and this one no longer exists"
        in out
    )
    report = json.loads((tmp_path / "artifacts" / RUN_ID / "diagnostics.json").read_text())
    assert report["retain"] is True


def test_a_pair_that_ended_before_its_pods_were_deployed_is_no_anomaly(
    job_cluster: FakeCluster, tmp_path: Path
) -> None:
    # A parameter ConfigError, a missing image or a Robot crash before deploy(): its own
    # error category caught it, and no pod of it ever existed.
    main(EXECUTE)

    report = json.loads((tmp_path / "artifacts" / RUN_ID / "diagnostics.json").read_text())
    assert report["anomalies"] == []
    assert report["retain"] is False
    assert report["pairs"][0]["started"] is False


def test_a_phase_10_that_raises_still_lets_cleanup_run(
    job_cluster: FakeCluster,
    robot: FakeRobotRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    robot.on_run = lambda: pair_pods(job_cluster)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug in phase 10")

    monkeypatch.setattr(commands, "capture", broken)

    main(EXECUTE)

    assert "could not record the run's diagnostics: a bug in phase 10" in capsys.readouterr().err
    assert len(removed_pair_pods(job_cluster)) == 2


def test_execute_removes_the_test_pods_then_releases_the_cluster(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    from dokimi.k8s.resources import POD  # noqa: PLC0415

    def phase_6() -> None:
        left_pod(job_cluster, f"run-{RUN_ID}-pair-001-client")
        left_pod(job_cluster, f"run-{RUN_ID}-pair-001-server", component="server")

    robot.on_run = phase_6

    main(EXECUTE)

    out = capsys.readouterr().out.splitlines()

    assert job_cluster.store.list(POD, f"dokimi.io/run-id={RUN_ID}", "dokimi-iperf2") == []
    assert "phase 11 (cleanup): removed 2 test pod(s)" in out
    # The artifacts stay last in the log, after the removal.
    assert out[-1] == f"::dokimi-artifacts end run={RUN_ID}::"
    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION in owner["metadata"]["annotations"]
    # Only pods: the definition and the owner stay, for attach and for cleanup.
    assert job_cluster.store.exists(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))


def test_only_the_test_pods_are_removed_before_the_claim_is_released(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    # A kept Prometheus's pod or a DaemonSet's carries the run's labels too. Neither is test
    # traffic: deleting one loses the history it is kept for, or is undone by its controller,
    # and waiting for it to go would hold the cluster until the claim expires.
    from dokimi.k8s.resources import POD  # noqa: PLC0415

    left_pod(job_cluster, f"run-{RUN_ID}-prometheus-abc12", component="prometheus")
    left_pod(job_cluster, f"run-{RUN_ID}-node-exporter-x7k2p", component="node-exporter")
    robot.on_run = lambda: left_pod(job_cluster, f"run-{RUN_ID}-pair-001-client")

    main(EXECUTE)

    remaining = job_cluster.store.list(POD, f"dokimi.io/run-id={RUN_ID}", "dokimi-iperf2")
    assert sorted(pod["metadata"]["name"] for pod in remaining) == [
        f"run-{RUN_ID}-node-exporter-x7k2p",
        f"run-{RUN_ID}-prometheus-abc12",
    ]
    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION in owner["metadata"]["annotations"]


def test_pods_that_cannot_be_deleted_keep_the_cluster_claimed(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    # A client that may still be sending must not let the next run measure next to it.
    left_pod(job_cluster, f"run-{RUN_ID}-pair-001-client")
    job_cluster.store.fail_deleting = True

    main(EXECUTE)
    err = capsys.readouterr().err

    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION not in (owner["metadata"].get("annotations") or {})
    assert "keeps its claim on the cluster until" in err
    # Removing the pods frees nothing by itself: the way out ends at the claim.
    assert f"dokimi cleanup {RUN_ID} --namespace dokimi-iperf2 --force" in err


def test_pods_that_never_go_away_keep_the_cluster_claimed(
    job_cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    left_pod(job_cluster, f"run-{RUN_ID}-pair-001-client")
    from dokimi.k8s.ownership import find_run_owned  # noqa: PLC0415

    lingering = find_run_owned(
        job_cluster.store, RunId(RUN_ID), namespace="dokimi-iperf2", kinds=(POD,)
    )
    monkeypatch.setattr(test_pods, "find_run_owned", lambda *_a, **_k: lingering)
    monkeypatch.setattr(commands, "POD_REMOVAL_TIMEOUT_SECONDS", 0.0)

    main(EXECUTE)

    owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    assert FINISHED_AT_ANNOTATION not in (owner["metadata"].get("annotations") or {})
    assert "were still there after 0s" in capsys.readouterr().err


# -- phase 11: what a run keeps ------------------------------------------------------------


def owner_annotations(fake: FakeCluster) -> dict[str, str]:
    owner = fake.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
    annotations: dict[str, str] = owner["metadata"].get("annotations") or {}
    return annotations


def test_a_passing_run_keeps_nothing(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded(robot, holds=True)
    robot.on_run = lambda: pair_pods(job_cluster)

    assert main(EXECUTE) == ExitCode.PASS

    assert len(removed_pair_pods(job_cluster)) == 2
    assert FINISHED_AT_ANNOTATION in owner_annotations(job_cluster)
    assert RETAINED_ANNOTATION not in owner_annotations(job_cluster)
    assert "kept for inspection" not in capsys.readouterr().out


def test_a_failed_run_keeps_its_test_pods_and_still_releases_the_cluster(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded(robot, holds=False)
    robot.on_run = lambda: pair_pods(job_cluster)

    assert main(EXECUTE) == ExitCode.FAIL

    assert removed_pair_pods(job_cluster) == []
    annotations = owner_annotations(job_cluster)
    assert FINISHED_AT_ANNOTATION in annotations
    assert annotations[RETAINED_ANNOTATION] == "fail"
    out = capsys.readouterr().out
    assert (
        f"phase 11 (cleanup): run kept for inspection (fail): run-{RUN_ID}-pair-001-client, "
        f"run-{RUN_ID}-pair-001-server, and its owner and definition" in out
    )
    assert f"dokimi cleanup {RUN_ID} --namespace dokimi-iperf2" in out


def test_an_anomaly_keeps_the_run_even_though_its_tests_passed(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    recorded(robot, holds=True)
    robot.on_run = lambda: server_lost(job_cluster)

    assert main(EXECUTE) == ExitCode.ERROR

    assert owner_annotations(job_cluster)[RETAINED_ANNOTATION] == "error"
    assert removed_pair_pods(job_cluster) == []


def test_a_test_that_may_still_be_sending_loses_its_pods_whatever_is_kept(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    # A timed-out client may still be sending, and the cluster is released next.
    robot.ends_with = TestError(ErrorCategory.TIMEOUT, "pair-001 did not complete")
    robot.on_run = lambda: pair_pods(job_cluster)

    assert main(EXECUTE) == ExitCode.ERROR

    assert len(removed_pair_pods(job_cluster)) == 2
    assert owner_annotations(job_cluster)[RETAINED_ANNOTATION] == "error"


def test_a_test_whose_pods_never_became_ready_keeps_them_for_inspection(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    # Pending pods send nothing, and their events say why they never started.
    robot.ends_with = TestError(ErrorCategory.SCHEDULING, "pair-001 could not be scheduled")
    robot.on_run = lambda: pair_pods(job_cluster)

    main(EXECUTE)

    assert removed_pair_pods(job_cluster) == []


@pytest.mark.parametrize(
    ("retain", "holds", "kept"),
    [("never", False, False), ("always", True, True), ("on-failure", True, False)],
)
def test_the_submitter_decides_when_a_run_keeps_its_evidence(
    job_cluster: FakeCluster, robot: FakeRobotRunner, retain: str, holds: bool, kept: bool
) -> None:
    recorded(robot, holds=holds)
    robot.on_run = lambda: pair_pods(job_cluster)

    main([*EXECUTE, "--retain", retain])

    assert (removed_pair_pods(job_cluster) == []) is kept
    assert (RETAINED_ANNOTATION in owner_annotations(job_cluster)) is kept


def test_a_run_that_keeps_its_evidence_still_removes_its_node_exporter(
    job_cluster: FakeCluster, robot: FakeRobotRunner
) -> None:
    # The kept Prometheus would go on scraping every node through it.
    recorded(robot, holds=False)

    main(EXECUTE)

    assert not job_cluster.store.list(
        DAEMON_SET, f"dokimi.io/run-id={RUN_ID},app.kubernetes.io/component=node-exporter"
    )


def test_a_run_that_stopped_in_its_preflight_keeps_nothing(
    job_cluster: FakeCluster, probe: FakeProbe
) -> None:
    # Its probe pods always go, and its diagnostics.json reaches the submitter with its log.
    probe.result = ProbeResult(ProbeCheck.CONNECTIVITY, "no UDP arrived")

    assert main(EXECUTE) == ExitCode.PREFLIGHT_FAILURE

    annotations = owner_annotations(job_cluster)
    assert FINISHED_AT_ANNOTATION in annotations
    assert RETAINED_ANNOTATION not in annotations


def test_a_preflight_answers_0_or_4_whatever_the_snapshot_found(
    job_cluster: FakeCluster, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug in phase 10")

    monkeypatch.setattr(commands, "capture", broken)

    assert main([*EXECUTE, "--preflight-only"]) == ExitCode.PASS
    assert "could not record the run's diagnostics" in capsys.readouterr().err


def test_an_owner_that_is_no_longer_the_runs_is_reported_not_raised(
    job_cluster: FakeCluster, robot: FakeRobotRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded(robot, holds=True)

    def relabelled() -> None:
        owner = job_cluster.store.get(CONFIG_MAP, "dokimi-iperf2", owner_name(RunId(RUN_ID)))
        owner["metadata"]["labels"] = {}
        job_cluster.store.replace(CONFIG_MAP, "dokimi-iperf2", owner)

    robot.on_run = relabelled

    assert main(EXECUTE) == ExitCode.PASS
    assert f"could not mark run {RUN_ID} finished" in capsys.readouterr().err


def test_a_node_exporter_left_by_a_crashed_run_is_named_with_its_cleanup(
    job_cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    crashed = "20251231-100000-aaaaaa"
    job_cluster.store.put(
        DAEMON_SET,
        "dokimi-iperf2",
        {
            "metadata": {
                "name": f"run-{crashed}-node-exporter",
                "labels": ownership_labels(RunId(crashed), component="node-exporter"),
            }
        },
    )

    main(EXECUTE)

    assert (
        f"was left behind by run {crashed}, and this run uses it. Once this run has ended, "
        f"remove what that run left with: dokimi cleanup {crashed} --namespace dokimi-iperf2"
        in capsys.readouterr().err
    )


# -- prometheus ---------------------------------------------------------------


def kept_prometheus(fake: FakeCluster, *, ready: bool = True) -> str:
    """A run's kept Prometheus, with the pod its Deployment runs. Returns the pod's name."""
    from dokimi.k8s.ownership import RunOwnership  # noqa: PLC0415
    from dokimi.metrics.managed import provision_managed_prometheus  # noqa: PLC0415

    provision_managed_prometheus(RunOwnership(fake.store, RunId(RUN_ID)), "dokimi-iperf2")
    name = f"run-{RUN_ID}-prometheus-7d9f6c5b8-x2kqp"
    fake.store.put(
        POD,
        "dokimi-iperf2",
        {
            "metadata": {
                "name": name,
                "labels": ownership_labels(RunId(RUN_ID), component="prometheus"),
            },
            "status": {
                "phase": "Running" if ready else "Pending",
                "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            },
        },
    )
    return name


def serves_http(body: bytes) -> Callable[[socket.socket], None]:
    """The far end of a port-forward, answering one HTTP request as Prometheus would."""

    def answer(channel: socket.socket) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = channel.recv(4096)
            if not chunk:
                return
            request += chunk
        channel.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
            + body
        )
        channel.close()

    return answer


def test_prometheus_forwards_a_local_port_to_the_runs_kept_prometheus(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    # Acceptance criterion: it opens the kept Prometheus through a port-forward, with no
    # kubectl, and prints its URL and the run's time range.
    submitted(cluster, RUN_ID)
    pod = kept_prometheus(cluster)
    cluster.forwarded = serves_http(b'{"status":"success"}')
    answers: list[bytes] = []

    def query(relay: PortRelay) -> None:
        host, port = relay.address
        with urllib.request.urlopen(f"http://{host}:{port}/api/v1/query?query=up", timeout=5) as r:
            answers.append(r.read())

    code = commands.open_prometheus(
        commands.Console(out=sys.stdout, err=sys.stderr),
        run_id=RUN_ID,
        connect_to=lambda **_kwargs: cluster,
        until=query,
    )

    assert code == ExitCode.PASS
    assert answers == [b'{"status":"success"}']
    assert cluster.forwards == [("dokimi-iperf2", pod, 9090)]
    out = capsys.readouterr().out
    assert f"run {RUN_ID}'s Prometheus: http://127.0.0.1:" in out
    assert "the run started 2026-01-01T10:00:00Z, and has not finished yet" in out
    assert f'its series carry run_id="{RUN_ID}"' in out
    assert "stopped forwarding" in out


def test_prometheus_says_when_the_run_ended(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    from dokimi.k8s.exclusivity import mark_finished  # noqa: PLC0415

    submitted(cluster, RUN_ID)
    kept_prometheus(cluster)
    mark_finished(
        cluster.store,
        RunId(RUN_ID),
        namespace="dokimi-iperf2",
        now=lambda: datetime(2026, 1, 1, 10, 3, 10, tzinfo=UTC),
    )

    commands.open_prometheus(
        commands.Console(out=sys.stdout, err=sys.stderr),
        run_id=RUN_ID,
        connect_to=lambda **_kwargs: cluster,
        until=lambda relay: None,
    )

    assert "started 2026-01-01T10:00:00Z, ended 2026-01-01T10:03:10Z" in capsys.readouterr().out


def test_a_run_whose_prometheus_was_removed_says_so_and_exits_3(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    submitted(cluster, RUN_ID)

    assert main(["prometheus", RUN_ID]) == ExitCode.CONFIG_ERROR
    assert (
        f"run {RUN_ID} has no Prometheus in dokimi-iperf2: it was removed, with dokimi "
        "cleanup --prometheus or by hand, or the run never deployed one"
    ) in capsys.readouterr().err
    assert cluster.forwards == []


def test_a_prometheus_without_a_ready_pod_is_not_forwarded_to(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    submitted(cluster, RUN_ID)
    kept_prometheus(cluster, ready=False)

    assert main(["prometheus", RUN_ID]) == ExitCode.INTERNAL
    assert "none of its pods is ready" in capsys.readouterr().err
    assert cluster.forwards == []


def test_a_refused_port_forward_is_reported_and_forwarding_goes_on(
    cluster: FakeCluster, capsys: pytest.CaptureFixture[str]
) -> None:
    # Without `create` on `pods/portforward`, each connection is refused, and says why.
    submitted(cluster, RUN_ID)
    kept_prometheus(cluster)

    def connect_once(relay: PortRelay) -> None:
        with socket.create_connection(relay.address, timeout=5) as connection:
            connection.sendall(b"GET / HTTP/1.1\r\n\r\n")
            with contextlib.suppress(OSError):
                connection.recv(1024)
        time.sleep(0.2)

    code = commands.open_prometheus(
        commands.Console(out=sys.stdout, err=sys.stderr),
        run_id=RUN_ID,
        connect_to=lambda **_kwargs: cluster,
        until=connect_once,
    )

    assert code == ExitCode.PASS
    assert "could not forward a connection: forwarding to port 9090" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["0", "65536", "http"])
def test_prometheus_refuses_a_port_that_isnt_one(port: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["prometheus", RUN_ID, "--port", port])

    assert exit_info.value.code == ExitCode.CONFIG_ERROR
