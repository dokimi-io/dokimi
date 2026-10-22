"""Phase 11 and standalone cleanup, against the in-memory API server.

What a run keeps, which of its test pods may stay, removing a run by its id or its age
without its own process, and what a submitter's fetch of the artifacts releases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from dokimi.cleanup import (
    Release,
    Retention,
    clean_up_run,
    quiet_tests,
    release_fetched_run,
    remove_test_pods,
    runs_older_than,
)
from dokimi.error_categories import ErrorCategory
from dokimi.k8s.ownership import (
    CLAIM_EXPIRES_AT_ANNOTATION,
    CLAIMED_AT_ANNOTATION,
    FINISHED_AT_ANNOTATION,
    RETAINED_ANNOTATION,
    format_timestamp,
    owner_manifest,
    owner_name,
    ownership_labels,
)
from dokimi.k8s.resources import CONFIG_MAP, DAEMON_SET, DEPLOYMENT, JOB, POD, Kind
from dokimi.run_identity import RunId
from dokimi.validation import Outcome, TestError, TestOutcome
from tests.fake_resources import FakeResources

NS = "dokimi-iperf2"
RUN = RunId("20261020-100000-aaaaaa")
OTHER = RunId("20261022-110000-bbbbbb")
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> FakeResources:
    return FakeResources()


def put(
    store: FakeResources,
    kind: Kind,
    name: str,
    run_id: RunId = RUN,
    *,
    component: str,
    test_id: str | None = None,
) -> None:
    labels = ownership_labels(run_id, test_id=test_id, component=component)
    store.put(kind, NS, {"metadata": {"name": name, "labels": labels}})


def owner(
    store: FakeResources, run_id: RunId = RUN, *, annotations: dict[str, str] | None = None
) -> None:
    manifest: dict[str, Any] = owner_manifest(run_id, NS)
    manifest["metadata"]["annotations"] = annotations or {}
    store.put(CONFIG_MAP, NS, manifest)


def claimed(
    *, expires: datetime, finished: bool = False, retained: str | None = None
) -> dict[str, str]:
    annotations = {
        CLAIMED_AT_ANNOTATION: format_timestamp(NOW - timedelta(minutes=5)),
        CLAIM_EXPIRES_AT_ANNOTATION: format_timestamp(expires),
    }
    if finished:
        annotations[FINISHED_AT_ANNOTATION] = format_timestamp(NOW - timedelta(minutes=1))
    if retained:
        annotations[RETAINED_ANNOTATION] = retained
    return annotations


def a_whole_run(store: FakeResources, run_id: RunId = RUN) -> None:
    """Everything a run leaves behind it, apart from its owner."""
    put(store, POD, f"run-{run_id}-pair-001-client", run_id, component="client", test_id="pair-001")
    put(store, POD, f"run-{run_id}-pair-001-server", run_id, component="server", test_id="pair-001")
    put(store, DAEMON_SET, f"run-{run_id}-node-exporter", run_id, component="node-exporter")
    put(store, CONFIG_MAP, f"run-{run_id}-definition", run_id, component="definition")
    put(store, DEPLOYMENT, f"run-{run_id}-prometheus", run_id, component="prometheus")
    put(store, JOB, f"run-{run_id}", run_id, component="orchestrator")


def names(store: FakeResources) -> set[str]:
    return {name for _, _, name in store.objects}


# -- what a run keeps ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("retention", "outcome", "reason"),
    [
        (Retention.ON_FAILURE, Outcome.PASS, None),
        (Retention.ON_FAILURE, Outcome.FAIL, "fail"),
        (Retention.ON_FAILURE, Outcome.ERROR, "error"),
        (Retention.ON_FAILURE, None, "stopped"),
        (Retention.ALWAYS, Outcome.PASS, "requested"),
        (Retention.ALWAYS, Outcome.FAIL, "fail"),
        (Retention.NEVER, Outcome.FAIL, None),
        (Retention.NEVER, None, None),
    ],
)
def test_a_run_keeps_its_evidence_when_it_did_not_pass_unless_told_otherwise(
    retention: Retention, outcome: Outcome | None, reason: str | None
) -> None:
    assert retention.reason(outcome) == reason


def recorded(test_id: str, category: ErrorCategory | None = None, *, error: bool = False) -> Any:
    document: dict[str, Any] = {"outcome": "error" if error else "fail", "error": None}
    if error:
        document["error"] = TestError(category, "x").to_dict()
    return TestOutcome(test_id, Outcome.ERROR if error else Outcome.FAIL, document)


def test_only_a_test_known_to_be_quiet_may_keep_its_pods() -> None:
    tests = [
        recorded("measured"),
        recorded("never-scheduled", ErrorCategory.SCHEDULING, error=True),
        recorded("no-image", ErrorCategory.IMAGE_PULL, error=True),
        recorded("no-result", ErrorCategory.METRICS_MISSING, error=True),
        recorded("timed-out", ErrorCategory.TIMEOUT, error=True),
        recorded("lost", ErrorCategory.TOOL_EXECUTION, error=True),
        recorded("surprise", None, error=True),
        TestOutcome("crashed", Outcome.ERROR, None),
    ]

    assert quiet_tests(tests) == {"measured", "never-scheduled", "no-image", "no-result"}


# -- removing test pods --------------------------------------------------------------


def test_test_pods_are_removed_except_those_kept(store: FakeResources) -> None:
    put(store, POD, "a-client", component="client", test_id="pair-001")
    put(store, POD, "b-client", component="client", test_id="pair-002")
    put(store, POD, "prometheus", component="prometheus")

    removal = remove_test_pods(store, RUN, NS, keep={"pair-001"})

    assert removal.complete
    assert [pod.name for pod in removal.removed] == ["b-client"]
    assert [pod.name for pod in removal.kept] == ["a-client"]
    assert names(store) == {"a-client", "prometheus"}


def test_a_pod_that_cannot_be_deleted_is_the_removals_problem(store: FakeResources) -> None:
    put(store, POD, "a-client", component="client", test_id="pair-001")
    store.fail_deleting = True

    removal = remove_test_pods(store, RUN, NS)

    assert not removal.complete
    assert removal.problem is not None
    assert "403 Forbidden" in removal.problem


# -- dokimi cleanup ---------------------------------------------------------------------


def test_cleanup_removes_exactly_that_runs_objects(store: FakeResources) -> None:
    owner(store, annotations=claimed(expires=NOW - timedelta(minutes=1), finished=True))
    a_whole_run(store)
    owner(store, OTHER)
    a_whole_run(store, OTHER)

    result = clean_up_run(store, RUN, NS, now=lambda: NOW)

    assert result.complete
    # The Job goes with its owner, through the garbage collector; the Prometheus stays.
    assert {name for name in names(store) if str(RUN) in name} == {
        f"run-{RUN}",
        f"run-{RUN}-prometheus",
    }
    assert [obj.name for obj in result.kept] == [f"run-{RUN}-prometheus"]
    assert len([name for name in names(store) if str(OTHER) in name]) == 7


def test_cleanup_removes_the_prometheus_only_when_asked(store: FakeResources) -> None:
    a_whole_run(store)

    clean_up_run(store, RUN, NS, remove_prometheus=True, now=lambda: NOW)

    assert names(store) == {f"run-{RUN}"}


def test_cleanup_refuses_a_run_that_still_holds_the_cluster(store: FakeResources) -> None:
    owner(store, annotations=claimed(expires=NOW + timedelta(minutes=30)))
    a_whole_run(store)

    result = clean_up_run(store, RUN, NS, now=lambda: NOW)

    assert result.refused is not None
    assert "still holds the cluster until" in result.refused
    assert "--force" in result.refused
    assert len(names(store)) == 7


def test_cleanup_with_force_removes_a_run_whose_job_is_gone(store: FakeResources) -> None:
    owner(store, annotations=claimed(expires=NOW + timedelta(minutes=30)))
    a_whole_run(store)

    result = clean_up_run(store, RUN, NS, force=True, now=lambda: NOW)

    assert result.complete
    assert owner_name(RUN) not in names(store)


def test_a_dry_run_lists_and_removes_nothing(store: FakeResources) -> None:
    a_whole_run(store)

    result = clean_up_run(store, RUN, NS, dry_run=True, now=lambda: NOW)

    assert result.deletion is None
    assert len(result.removable) == 4
    assert len(names(store)) == 6


def test_runs_older_than_an_age_are_found_by_anything_they_left(store: FakeResources) -> None:
    # The older one has lost its owner: only its node-exporter is left.
    put(store, DAEMON_SET, "old-exporter", RUN, component="node-exporter")
    owner(store, OTHER)

    assert runs_older_than(store, NS, timedelta(hours=24), now=lambda: NOW) == [RUN]
    assert runs_older_than(store, NS, timedelta(minutes=30), now=lambda: NOW) == [RUN, OTHER]


# -- after the artifacts are fetched -----------------------------------------------------


def test_a_run_that_kept_nothing_goes_once_its_artifacts_are_fetched(store: FakeResources) -> None:
    owner(store, annotations=claimed(expires=NOW, finished=True))

    assert release_fetched_run(store, RUN, NS) is Release.RUN
    # Its Job and definition follow their owner, through the garbage collector.
    assert owner_name(RUN) not in names(store)


def test_a_run_that_never_claimed_the_cluster_goes_too(store: FakeResources) -> None:
    # Refused, because another run held the cluster: there is nothing of it to inspect.
    owner(store)

    assert release_fetched_run(store, RUN, NS) is Release.RUN


@pytest.mark.parametrize(
    "annotations",
    [
        claimed(expires=NOW, finished=True, retained="fail"),
        claimed(expires=NOW + timedelta(minutes=30)),
    ],
    ids=["kept its evidence", "never marked finished"],
)
def test_otherwise_only_its_job_goes(store: FakeResources, annotations: dict[str, str]) -> None:
    owner(store, annotations=annotations)
    put(store, JOB, f"run-{RUN}", component="orchestrator")
    put(store, JOB, f"run-{OTHER}", OTHER, component="orchestrator")

    assert release_fetched_run(store, RUN, NS) is Release.JOB

    assert names(store) == {owner_name(RUN), f"run-{OTHER}"}
