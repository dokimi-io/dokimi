"""Run exclusivity: one active run per cluster, including when two start at once.

Two of M1-05's acceptance criteria live here: a second run started while one is active is
refused with a message naming the active run, and two simultaneous submissions result in
exactly one run starting.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from dokimi.k8s.exclusivity import (
    LOCK_NAME,
    RunAlreadyActiveError,
    RunExclusivityError,
    claim_run,
)
from dokimi.k8s.ownership import (
    ORCHESTRATOR_COMPONENT,
    RunOwnership,
    delete_run_owned,
    owner_name,
    ownership_labels,
)
from dokimi.k8s.resources import CONFIG_MAP, LEASE, POD, Kind, Manifest
from dokimi.run_identity import RunId, new_run_id
from tests.fake_resources import FakeResources

FIRST = RunId("20260924-143012-k3f9x2")
SECOND = RunId("20260924-143013-p7m2q8")
NAMESPACE = "dokimi-iperf2"
LOCK_NAMESPACE = "dokimi"


@pytest.fixture
def cluster() -> FakeResources:
    return FakeResources()


def claim(cluster: FakeResources, run_id: RunId, **kwargs: object) -> RunOwnership:
    return claim_run(cluster, run_id, namespace=NAMESPACE, **kwargs)  # type: ignore[arg-type]


def test_a_claimed_run_owns_its_namespace_and_releases_the_lock(cluster: FakeResources) -> None:
    ownership = claim(cluster, FIRST)

    assert ownership.run_id == FIRST
    assert cluster.exists(CONFIG_MAP, NAMESPACE, owner_name(FIRST))
    assert not cluster.exists(LEASE, LOCK_NAMESPACE, LOCK_NAME)


def test_a_second_run_is_refused_naming_the_active_one(cluster: FakeResources) -> None:
    claim(cluster, FIRST)

    with pytest.raises(RunAlreadyActiveError) as refused:
        claim(cluster, SECOND)

    assert refused.value.active_run_ids == [str(FIRST)]
    message = str(refused.value)
    assert f"refusing to start run {SECOND}" in message
    assert f"run {FIRST} already active" in message
    assert f"dokimi.io/run-id={FIRST}" in message
    assert not cluster.exists(CONFIG_MAP, NAMESPACE, owner_name(SECOND))


def test_exclusivity_spans_every_namespace(cluster: FakeResources) -> None:
    cluster.namespaces.add("dokimi-irtt")
    claim_run(cluster, FIRST, namespace="dokimi-irtt")

    with pytest.raises(RunAlreadyActiveError, match=str(FIRST)):
        claim(cluster, SECOND)


def test_leftovers_of_a_crashed_run_refuse_the_next_one(cluster: FakeResources) -> None:
    # A run that died after claiming, with no owner left — only a stray workload.
    cluster.put(
        POD,
        "default",
        {"metadata": {"name": "iperf2-client", "labels": ownership_labels(FIRST)}},
    )

    with pytest.raises(RunAlreadyActiveError, match="Pod/iperf2-client in default"):
        claim(cluster, SECOND)


def test_the_run_can_start_again_once_the_previous_one_is_cleaned_up(
    cluster: FakeResources,
) -> None:
    claim(cluster, FIRST)
    delete_run_owned(cluster, FIRST)

    claim(cluster, SECOND)


def test_unrelated_objects_and_orchestrator_jobs_do_not_block(cluster: FakeResources) -> None:
    cluster.put(POD, NAMESPACE, {"metadata": {"name": "someone-elses"}})
    # The *other* submission's own orchestrator pod: labelled with its run_id so the CLI
    # can reattach, but not a resource that run created.
    cluster.put(
        POD,
        LOCK_NAMESPACE,
        {
            "metadata": {
                "name": "orchestrator",
                "labels": ownership_labels(SECOND, component=ORCHESTRATOR_COMPONENT),
            }
        },
    )

    claim(cluster, FIRST)


def _submit_simultaneously(cluster: FakeResources, run_ids: list[RunId]) -> dict[str, object]:
    barrier = threading.Barrier(len(run_ids))
    outcomes: dict[str, object] = {}

    def submit(run_id: RunId) -> None:
        barrier.wait()
        try:
            outcomes[str(run_id)] = claim(cluster, run_id)
        except RunAlreadyActiveError as error:
            outcomes[str(run_id)] = error

    threads = [threading.Thread(target=submit, args=(run_id,)) for run_id in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


@pytest.mark.parametrize("attempt", range(25))
def test_two_simultaneous_submissions_start_exactly_one_run(attempt: int) -> None:
    outcomes = _submit_simultaneously(FakeResources(), [new_run_id(), new_run_id()])

    started = [rid for rid, outcome in outcomes.items() if isinstance(outcome, RunOwnership)]
    refused = [o for o in outcomes.values() if isinstance(o, RunAlreadyActiveError)]
    assert len(started) == 1
    assert len(refused) == 1
    assert refused[0].active_run_ids == started


def test_the_lock_closes_the_window_between_check_and_claim(cluster: FakeResources) -> None:
    """The race the lock exists for, forced deterministically.

    FIRST is paused after taking the lock and before creating its owner — exactly where a
    check-only design would let SECOND see an empty cluster and start as well.
    """
    first_holds_lock = threading.Event()
    second_done = threading.Event()

    def pause_first(kind: Kind, namespace: str, body: Manifest) -> None:
        if kind == CONFIG_MAP and body["metadata"]["name"] == owner_name(FIRST):
            first_holds_lock.set()
            second_done.wait(timeout=5)

    cluster.before_create = pause_first
    first_result: list[object] = []
    first = threading.Thread(target=lambda: first_result.append(claim(cluster, FIRST)))
    first.start()
    assert first_holds_lock.wait(timeout=5)

    with pytest.raises(RunAlreadyActiveError, match="run-start lock") as refused:
        claim(cluster, SECOND)
    second_done.set()
    first.join()

    assert refused.value.active_run_ids == [str(FIRST)]
    assert isinstance(first_result[0], RunOwnership)


def _stale_lock(cluster: FakeResources, holder: RunId, *, age: timedelta, seconds: int) -> None:
    stamp = (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cluster.put(
        LEASE,
        LOCK_NAMESPACE,
        {
            "metadata": {"name": LOCK_NAME, "labels": ownership_labels(holder)},
            "spec": {
                "holderIdentity": str(holder),
                "leaseDurationSeconds": seconds,
                "acquireTime": stamp,
                "renewTime": stamp,
            },
        },
    )


def test_a_lock_left_by_a_crashed_start_is_taken_over_once_expired(
    cluster: FakeResources,
) -> None:
    _stale_lock(cluster, FIRST, age=timedelta(minutes=5), seconds=60)

    claim(cluster, SECOND)

    assert cluster.exists(CONFIG_MAP, NAMESPACE, owner_name(SECOND))


def test_a_live_lock_refuses_even_with_nothing_else_in_the_cluster(
    cluster: FakeResources,
) -> None:
    _stale_lock(cluster, FIRST, age=timedelta(seconds=5), seconds=60)

    with pytest.raises(RunAlreadyActiveError, match=str(FIRST)):
        claim(cluster, SECOND)


def test_a_claim_that_loses_its_lock_midway_withdraws(cluster: FakeResources) -> None:
    """If the lock expired and changed hands while we were stalled, back out."""

    def take_over(kind: Kind, namespace: str, body: Manifest) -> None:
        if kind == CONFIG_MAP and body["metadata"]["name"] == owner_name(FIRST):
            lease = cluster.get(LEASE, LOCK_NAMESPACE, LOCK_NAME)
            lease["spec"]["holderIdentity"] = str(SECOND)
            cluster.replace(LEASE, LOCK_NAMESPACE, lease)

    cluster.before_create = take_over

    with pytest.raises(RunAlreadyActiveError, match="taken over"):
        claim(cluster, FIRST)

    assert not cluster.exists(CONFIG_MAP, NAMESPACE, owner_name(FIRST))
    # The lock is someone else's now, so it is not ours to release.
    assert cluster.exists(LEASE, LOCK_NAMESPACE, LOCK_NAME)


def test_the_lock_is_released_when_the_claim_is_refused(cluster: FakeResources) -> None:
    claim(cluster, FIRST)
    with pytest.raises(RunAlreadyActiveError):
        claim(cluster, SECOND)

    assert not cluster.exists(LEASE, LOCK_NAMESPACE, LOCK_NAME)


def test_a_missing_lock_namespace_is_a_clear_error() -> None:
    cluster = FakeResources(namespaces={NAMESPACE})

    with pytest.raises(RunExclusivityError, match="'dokimi' does not exist"):
        claim(cluster, FIRST)


def test_the_lock_namespace_is_configurable(cluster: FakeResources) -> None:
    cluster.namespaces.add("tools")
    seen: list[str] = []

    def record(kind: Kind, namespace: str, body: Manifest) -> None:
        if kind == LEASE:
            seen.append(namespace)

    cluster.before_create = record

    claim(cluster, FIRST, lock_namespace="tools")

    assert seen == ["tools"]
