"""Submitting, following and reattaching.

Submitting, following and reattaching are all testable without a cluster, because all
three are statements about which calls happen and what they are given.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from dokimi.cli.exit_codes import ExitCode
from dokimi.images import ImageSource
from dokimi.k8s.cluster import ClusterError
from dokimi.k8s.identity import RUN_ID_LABEL, run_selector
from dokimi.k8s.submission import (
    CONTAINER_NAME,
    RUN_OUTCOMES,
    RunNotFound,
    Submission,
    follow,
    plan_submission,
    submit,
)
from dokimi.model import (
    Bandwidth,
    Comparison,
    ConfigError,
    Criterion,
    Duration,
    Metric,
    Protocol,
    TestDefinition,
    TestType,
    Unit,
)
from dokimi.testtypes.interface import WorkloadImage
from dokimi.validation import Outcome
from tests.fake_cluster import FakeCluster

RUN_ID = "20260101-100000-4f2a9c"
IMAGE = "ghcr.io/dokimi-io/orchestrator@sha256:" + "ab" * 32
#: The images a run deploys, as ``submit`` resolves them.
RUN_IMAGES = {
    "iperf2": WorkloadImage(
        "ghcr.io/dokimi-io/iperf2@sha256:" + "cd" * 32, ImageSource.RELEASE_DEFAULT
    )
}


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
        source="apiVersion: dokimi.io/v1alpha1\n",
        source_path=Path("pair.yaml"),
        image=IMAGE,
        images=RUN_IMAGES,
        cluster="test-cluster",
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
    assert submission.namespace == "dokimi-iperf2"
    assert len(submission.manifests) == 3


def test_planning_generates_a_run_id_when_not_given(definition: TestDefinition) -> None:
    submission = plan_submission(
        definition,
        source="",
        source_path=Path("p.yaml"),
        image=IMAGE,
        images=RUN_IMAGES,
        cluster="test-cluster",
    )

    assert submission.run_id


def test_submit_creates_the_owner_first_and_binds_the_rest_to_it(
    definition: TestDefinition,
) -> None:
    cluster = FakeCluster()

    submit(cluster, _plan(definition))

    # A run never creates a namespace: only the owner, the definition, the Job.
    assert cluster.created_kinds == ["ConfigMap", "ConfigMap", "Job"]
    for manifest in cluster.created[1:]:
        assert manifest["metadata"]["ownerReferences"][0]["uid"] == cluster.owner_uid


def test_submit_refuses_an_owner_without_a_uid(definition: TestDefinition) -> None:
    # Without a uid there is nothing for the garbage collector to cascade from, so the
    # cleanup backstop would silently not exist.
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

    # The Job's exit status is the verdict, so it is propagated, not
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


# -- review of #27 --------------------------------------------------------------


@pytest.mark.parametrize("bad", ["My Run", "a" * 70, "run,id=x", "20260101-100000-4f2a9c,x=y"])
def test_a_given_run_id_is_validated_before_anything_is_rendered(
    definition: TestDefinition, bad: str
) -> None:
    # The id becomes a label value, part of every object name and a selector term. A bad
    # one used to reach the API server as a 422 (exit 5 instead of 3), and `,` or `=`
    # would have changed what the selector matched.
    with pytest.raises(ConfigError, match="malformed run id"):
        plan_submission(
            definition,
            source="",
            source_path=Path("p.yaml"),
            image=IMAGE,
            images=RUN_IMAGES,
            cluster="test-cluster",
            run_id=bad,
        )


def test_submitting_does_not_change_the_submission(definition: TestDefinition) -> None:
    # `Submission` is frozen, but its nested dictionaries were not: binding the owner uid
    # in place changed what a dry run of the same submission would print afterwards.
    submission = _plan(definition)
    before = copy.deepcopy(submission.manifests)

    submit(FakeCluster(), submission)

    assert submission.manifests == before


def test_a_failed_submission_removes_its_owner(definition: TestDefinition) -> None:
    # The owner was created, the Job was refused. The owner is unclaimed, so it would not
    # block later runs, but it is debris, removed by name and uid.
    cluster = FakeCluster(fail_on_kind="Job")

    with pytest.raises(ClusterError, match="422"):
        submit(cluster, _plan(definition))

    assert ("ConfigMap", "dokimi-iperf2", f"run-{RUN_ID}-owner") in cluster.deleted
    assert not cluster.store.objects


def test_a_failed_cleanup_is_reported_with_the_command_to_finish_it(
    definition: TestDefinition,
) -> None:
    # Cleanup failures are reported with what remains, never swallowed. The
    # original error is still the one raised.
    cluster = FakeCluster(fail_on_kind="Job", fail_deleting=True)

    with pytest.raises(ClusterError, match="422") as error:
        submit(cluster, _plan(definition))

    notes = " ".join(error.value.__notes__)
    assert "kubectl delete configmap,job" in notes
    assert run_selector(RUN_ID) in notes


def test_every_run_outcome_matches_the_cli_table() -> None:
    # Two modules name the exit codes; this is what keeps them the same list.
    assert frozenset(code.value for code in ExitCode if code.value <= 5) == RUN_OUTCOMES


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (Outcome.PASS, ExitCode.PASS),
        (Outcome.FAIL, ExitCode.FAIL),
        (Outcome.ERROR, ExitCode.ERROR),
        (None, ExitCode.INTERNAL),
    ],
)
def test_a_completed_runs_outcome_is_its_exit_code(outcome: Outcome | None, code: int) -> None:
    assert ExitCode.of(outcome) == code


@pytest.mark.parametrize("code", sorted(RUN_OUTCOMES))
def test_a_run_outcome_is_propagated_unchanged(code: int) -> None:
    cluster = FakeCluster(exit_code=code)

    assert follow(cluster, namespace="ns", run_id=RUN_ID, write=_ignore, sleep=_never_sleep) == code


@pytest.mark.parametrize(
    ("code", "reason", "expected"),
    [
        (137, "OOMKilled", "OOMKilled"),
        (137, "Error", "SIGKILL"),
        (143, "Error", "SIGTERM"),
        (139, "Error", "signal 11"),
        (42, "Error", "outside the outcome table"),
    ],
)
def test_an_exit_code_outside_the_table_is_not_passed_off_as_a_verdict(
    code: int, reason: str, expected: str
) -> None:
    # 137 passed straight through would reach CI as a number that means nothing in
    # the exit-code table. It is reported as what it is, and the CLI exits 5.
    cluster = FakeCluster(exit_code=code, termination_reason=reason)

    with pytest.raises(ClusterError, match=expected) as error:
        follow(cluster, namespace="ns", run_id=RUN_ID, write=_ignore, sleep=_never_sleep)

    assert "without a verdict" in str(error.value)


_DEADLINE = {"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}


def test_a_deadline_is_recognised_when_the_job_has_deleted_the_pod() -> None:
    # The deadline is on the Job, and the Job controller deletes the pod when it passes,
    # so the pod-level check this replaced could never fire. The Job's conditions can.
    cluster = FakeCluster(has_pod_after_stream=False, job_conditions=[_DEADLINE])

    with pytest.raises(ClusterError, match="activeDeadlineSeconds"):
        follow(cluster, namespace="ns", run_id=RUN_ID, write=_ignore, sleep=_never_sleep)


def test_a_deadline_is_recognised_when_the_pod_was_killed_for_it() -> None:
    # Timing can leave the pod long enough to report the SIGTERM it was killed with.
    cluster = FakeCluster(exit_code=143, termination_reason="Error", job_conditions=[_DEADLINE])

    with pytest.raises(ClusterError, match="activeDeadlineSeconds"):
        follow(cluster, namespace="ns", run_id=RUN_ID, write=_ignore, sleep=_never_sleep)


def test_a_vanished_pod_without_a_deadline_says_so() -> None:
    cluster = FakeCluster(has_pod_after_stream=False)

    with pytest.raises(ClusterError, match="disappeared before it reported"):
        follow(cluster, namespace="ns", run_id=RUN_ID, write=_ignore, sleep=_never_sleep)


def test_a_dropped_log_stream_points_back_at_attach() -> None:
    class DroppingCluster(FakeCluster):
        def stream_logs(self, namespace: str, pod: str, container: str) -> Iterator[str]:
            yield "phase 2"
            raise ClusterError("streaming logs: lost contact with the Kubernetes API server")

    with pytest.raises(ClusterError) as error:
        follow(DroppingCluster(), namespace="ns", run_id=RUN_ID, write=_ignore, sleep=_never_sleep)

    assert f"dokimi attach {RUN_ID}" in " ".join(error.value.__notes__)


def _ignore(_line: str) -> None:
    return None
