"""Submitting, following and reattaching (ADR-01-06).

The three properties ADR-01-06 asks for are all testable without a cluster, because all
three are statements about which calls happen and what they are given.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from test_orchestrator.k8s.cluster import ClusterError
from test_orchestrator.k8s.identity import RUN_ID_LABEL
from test_orchestrator.k8s.submission import (
    CONTAINER_NAME,
    RunNotFound,
    Submission,
    follow,
    plan_submission,
    submit,
)
from test_orchestrator.model import (
    Bandwidth,
    Comparison,
    Criterion,
    Duration,
    Metric,
    Protocol,
    TestDefinition,
    TestType,
    Unit,
)
from tests.fake_cluster import FakeCluster

RUN_ID = "20260101T100000Z-4f2a9c"
IMAGE = "ghcr.io/test-orchestrator/orchestrator@sha256:" + "ab" * 32


@pytest.fixture
def definition() -> TestDefinition:
    return TestDefinition(
        name="pair",
        source_node="worker-01",
        target_node="worker-02",
        test_type=TestType.IPERF2,
        protocol=Protocol.UDP,
        duration=Duration.parse("30s"),
        target_bandwidth=Bandwidth.parse("1G"),
        criteria=(
            Criterion(
                metric=Metric.THROUGHPUT,
                comparison=Comparison.AT_LEAST,
                threshold=Decimal(950),
                unit=Unit.MEGABITS_PER_SECOND,
            ),
        ),
    )


def _plan(definition: TestDefinition, namespace: str | None = None) -> Submission:
    return plan_submission(
        definition,
        source="apiVersion: test-orchestrator/v1alpha1\n",
        source_path=Path("pair.yaml"),
        image=IMAGE,
        run_id=RUN_ID,
        namespace=namespace,
    )


def _never_sleep(_seconds: float) -> None:
    return None


def test_planning_needs_no_cluster(definition: TestDefinition) -> None:
    # This is the whole of --dry-run: a pure function, so there is no client to pass and
    # no way for the rendering path to reach one by accident.
    submission = _plan(definition)

    assert submission.run_id == RUN_ID
    assert submission.namespace == "test-orchestrator-iperf2"
    assert len(submission.manifests) == 3


def test_planning_generates_a_run_id_when_not_given(definition: TestDefinition) -> None:
    submission = plan_submission(definition, source="", source_path=Path("p.yaml"), image=IMAGE)

    assert submission.run_id


def test_submit_creates_the_owner_first_and_binds_the_rest_to_it(
    definition: TestDefinition,
) -> None:
    cluster = FakeCluster()

    submit(cluster, _plan(definition))

    assert cluster.namespaces == ["test-orchestrator-iperf2"]
    assert cluster.created_kinds == ["ConfigMap", "ConfigMap", "Job"]
    for manifest in cluster.created[1:]:
        assert manifest["metadata"]["ownerReferences"][0]["uid"] == cluster.owner_uid


def test_submit_refuses_an_owner_without_a_uid(definition: TestDefinition) -> None:
    # Without a uid there is nothing for the garbage collector to cascade from, so the
    # backstop ADR-05-02 relies on would silently not exist.
    cluster = FakeCluster(owner_uid="")

    with pytest.raises(ClusterError, match="without a uid"):
        submit(cluster, _plan(definition))


def test_following_streams_the_log_and_returns_the_containers_exit_code() -> None:
    cluster = FakeCluster(log_lines=["phase 2", "phase 3"], exit_code=1)
    written: list[str] = []

    code = follow(
        cluster,
        namespace="ns",
        run_id=RUN_ID,
        write=written.append,
        sleep=_never_sleep,
    )

    # ADR-01-08 makes the Job's exit status the verdict, so it is propagated, not
    # reinterpreted: 1 means the run completed and something failed.
    assert code == 1
    assert written == ["phase 2", "phase 3"]
    assert cluster.streamed == [("ns", cluster.pod_name, CONTAINER_NAME)]


def test_following_finds_the_pod_by_label_not_by_name() -> None:
    # This is what makes reattach work from a process that never submitted anything.
    cluster = FakeCluster()

    follow(cluster, namespace="ns", run_id=RUN_ID, write=lambda _line: None, sleep=_never_sleep)

    assert all(f"{RUN_ID_LABEL}={RUN_ID}" in selector for selector in cluster.selectors)


def test_following_waits_for_a_pending_pod() -> None:
    # Logs cannot be read from a Pending pod, and a pod is Pending for a while after the
    # Job exists.
    cluster = FakeCluster(phases=["Pending", "Pending", "Running"])

    follow(cluster, namespace="ns", run_id=RUN_ID, write=lambda _line: None, sleep=_never_sleep)

    assert cluster.streamed


def test_following_waits_for_the_exit_code_after_the_stream_ends() -> None:
    # The stream closing and the status reporting a terminated container are separate
    # events.
    cluster = FakeCluster(exit_code=2, terminated_after=2)

    code = follow(
        cluster, namespace="ns", run_id=RUN_ID, write=lambda _line: None, sleep=_never_sleep
    )

    assert code == 2


def test_an_unknown_run_is_reported_as_missing_not_as_a_timeout() -> None:
    cluster = FakeCluster(has_pod=False, has_job=False)

    with pytest.raises(RunNotFound, match="no run"):
        follow(cluster, namespace="ns", run_id=RUN_ID, write=lambda _line: None, sleep=_never_sleep)


def test_a_job_that_never_schedules_a_pod_times_out_with_a_way_back() -> None:
    cluster = FakeCluster(has_pod=False, has_job=True)
    clock = iter([0.0, 0.0, 1.0, 2.0, 999.0, 1000.0])

    with pytest.raises(ClusterError, match="attach"):
        follow(
            cluster,
            namespace="ns",
            run_id=RUN_ID,
            write=lambda _line: None,
            timeout=10.0,
            sleep=_never_sleep,
            clock=lambda: next(clock),
        )
