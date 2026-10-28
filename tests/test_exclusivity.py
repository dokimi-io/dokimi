"""Run exclusivity: one active run per cluster, including when two start at once.

M1-05's acceptance criteria and adrs#63's live here: a second run started while one is
active is refused with a message naming the active run; two simultaneous submissions
start exactly one run — including through ``submit``, not only through the claim; and a
refused, crashed or finished run does not block later runs indefinitely (ADR-05-13).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dokimi.formats import parse_file
from dokimi.images import ImageSource
from dokimi.k8s.exclusivity import (
    LOCK_NAME,
    REFUSAL_REASON,
    RunAlreadyActiveError,
    RunExclusivityError,
    claim_expiry,
    claim_run,
    find_active_runs,
    mark_finished,
)
from dokimi.k8s.manifests import resource_name
from dokimi.k8s.ownership import (
    CLAIM_EXPIRES_AT_ANNOTATION,
    CLAIMED_AT_ANNOTATION,
    FINISHED_AT_ANNOTATION,
    OWNER_COMPONENT,
    OwnershipError,
    RunOwnership,
    claim_of,
    format_timestamp,
    owner_manifest,
    owner_name,
    ownership_labels,
)
from dokimi.k8s.resources import CONFIG_MAP, DEPLOYMENT, JOB, LEASE, POD, Kind, Manifest
from dokimi.k8s.submission import plan_submission, submit
from dokimi.run_identity import RunId, new_run_id
from dokimi.testtypes.interface import WorkloadImage
from tests.fake_cluster import FakeCluster
from tests.fake_resources import FakeResources

#: The images a run deploys, as ``submit`` resolves them (ADR-04-08).
RUN_IMAGES = {
    "iperf2": WorkloadImage(
        "ghcr.io/dokimi-io/iperf2@sha256:" + "cd" * 32, ImageSource.RELEASE_DEFAULT
    )
}

FIRST = RunId("20260924-143012-k3f9x2")
SECOND = RunId("20260924-143013-p7m2q8")
NAMESPACE = "dokimi-iperf2"
LOCK_NAMESPACE = "dokimi"
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "worker-01-to-worker-02-udp.yaml"


def later(minutes: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


@pytest.fixture
def cluster() -> FakeResources:
    return FakeResources()


def claim(cluster: FakeResources, run_id: RunId, **kwargs: object) -> RunOwnership:
    """Claim as the Job does: ``submit`` has already created the run's owner."""
    kwargs.setdefault("namespace", NAMESPACE)
    kwargs.setdefault("expires_at", later())
    namespace = str(kwargs["namespace"])
    if not cluster.exists(CONFIG_MAP, namespace, owner_name(run_id)):
        put_owner(cluster, run_id, namespace=namespace)
    return claim_run(cluster, run_id, **kwargs)  # type: ignore[arg-type]


def owner(cluster: FakeResources, run_id: RunId, namespace: str = NAMESPACE) -> Manifest:
    return cluster.get(CONFIG_MAP, namespace, owner_name(run_id))


def put_owner(
    cluster: FakeResources, run_id: RunId, *, namespace: str = NAMESPACE, **annotations: str
) -> None:
    body = owner_manifest(run_id, namespace)
    body["metadata"]["annotations"] = annotations
    cluster.put(CONFIG_MAP, namespace, body)


# -- claiming --------------------------------------------------------------------------


def test_a_claim_is_written_on_the_owner_and_the_lock_is_released(
    cluster: FakeResources,
) -> None:
    expires = later()

    ownership = claim(cluster, FIRST, expires_at=expires)

    assert ownership.run_id == FIRST
    annotations = owner(cluster, FIRST)["metadata"]["annotations"]
    assert CLAIMED_AT_ANNOTATION in annotations
    assert annotations[CLAIM_EXPIRES_AT_ANNOTATION] == format_timestamp(expires)
    assert not cluster.exists(LEASE, LOCK_NAMESPACE, LOCK_NAME)


def test_the_owner_submit_created_is_claimed_not_replaced(cluster: FakeResources) -> None:
    # In the Job the owner already exists; its uid must survive, since the definition
    # and the Job reference it.
    put_owner(cluster, FIRST)
    uid = owner(cluster, FIRST)["metadata"]["uid"]

    claim(cluster, FIRST)

    assert owner(cluster, FIRST)["metadata"]["uid"] == uid
    assert claim_of(owner(cluster, FIRST)).claimed_at is not None


def test_a_missing_owner_is_a_clear_error_and_is_not_created(cluster: FakeResources) -> None:
    # submit always creates the owner, and the Job's role could not create one.
    with pytest.raises(OwnershipError, match="does not exist"):
        claim_run(cluster, FIRST, namespace=NAMESPACE, expires_at=later())

    assert not cluster.exists(CONFIG_MAP, NAMESPACE, owner_name(FIRST))
    assert not cluster.exists(LEASE, LOCK_NAMESPACE, LOCK_NAME)


def test_an_owner_that_is_not_this_runs_is_not_adopted(cluster: FakeResources) -> None:
    cluster.put(CONFIG_MAP, NAMESPACE, {"metadata": {"name": owner_name(FIRST)}})

    with pytest.raises(OwnershipError, match="refusing to adopt"):
        claim(cluster, FIRST)


def test_a_second_run_is_refused_naming_the_active_one(cluster: FakeResources) -> None:
    claim(cluster, FIRST)

    with pytest.raises(RunAlreadyActiveError) as refused:
        claim(cluster, SECOND)

    assert refused.value.active_run_ids == [str(FIRST)]
    assert refused.value.reason == REFUSAL_REASON
    message = str(refused.value)
    assert f"refusing to start run {SECOND} ({REFUSAL_REASON})" in message
    assert f"run {FIRST} already active" in message
    assert f"ConfigMap {NAMESPACE}/{owner_name(FIRST)}" in message
    assert "claim expires at" in message
    assert f"dokimi cleanup {FIRST} --namespace {NAMESPACE} --force" in message
    assert claim_of(owner(cluster, SECOND)).claimed_at is None


def test_exclusivity_spans_every_namespace(cluster: FakeResources) -> None:
    cluster.namespaces.add("dokimi-irtt")
    claim(cluster, FIRST, namespace="dokimi-irtt")

    with pytest.raises(RunAlreadyActiveError, match=str(FIRST)):
        claim(cluster, SECOND)


# -- what does not count as an active run (ADR-05-13) ----------------------------------


def test_an_unclaimed_owner_does_not_block(cluster: FakeResources) -> None:
    # A submission that was refused, or whose Job never started, leaves this behind.
    put_owner(cluster, FIRST)

    claim(cluster, SECOND)


def test_a_finished_run_does_not_block_even_with_its_evidence_retained(
    cluster: FakeResources,
) -> None:
    claim(cluster, FIRST)
    cluster.put(POD, NAMESPACE, {"metadata": {"name": "client", "labels": ownership_labels(FIRST)}})
    mark_finished(cluster, FIRST, namespace=NAMESPACE)

    claim(cluster, SECOND)

    assert cluster.exists(POD, NAMESPACE, "client")


def test_a_crashed_runs_claim_stops_counting_when_it_expires(cluster: FakeResources) -> None:
    now = datetime.now(UTC)
    put_owner(
        cluster,
        FIRST,
        **{
            CLAIMED_AT_ANNOTATION: format_timestamp(now - timedelta(hours=2)),
            CLAIM_EXPIRES_AT_ANNOTATION: format_timestamp(now - timedelta(minutes=1)),
        },
    )

    claim(cluster, SECOND)


def test_a_crashed_run_still_blocks_until_its_claim_expires(cluster: FakeResources) -> None:
    put_owner(
        cluster,
        FIRST,
        **{
            CLAIMED_AT_ANNOTATION: format_timestamp(datetime.now(UTC)),
            CLAIM_EXPIRES_AT_ANNOTATION: format_timestamp(later(5)),
        },
    )

    with pytest.raises(RunAlreadyActiveError, match=str(FIRST)):
        claim(cluster, SECOND)


@pytest.mark.parametrize("expiry", [None, "", "next tuesday"])
def test_a_claim_without_a_readable_expiry_does_not_block(
    cluster: FakeResources, expiry: str | None
) -> None:
    # ADR-05-13: active only while claim-expires-at is in the future. Only a hand edit
    # produces this, since the claim writes both annotations in one update, and it must
    # not block the cluster until someone deletes the owner.
    annotations = {CLAIMED_AT_ANNOTATION: format_timestamp(datetime.now(UTC))}
    if expiry is not None:
        annotations[CLAIM_EXPIRES_AT_ANNOTATION] = expiry
    put_owner(cluster, FIRST, **annotations)

    claim(cluster, SECOND)


def test_workloads_and_a_kept_prometheus_are_never_evidence(cluster: FakeResources) -> None:
    # A stray workload, and a managed Prometheus ADR-03-02 keeps by default: both carry
    # the run's labels, neither is an owner, so neither refuses the next run.
    cluster.put(POD, "default", {"metadata": {"name": "iperf2", "labels": ownership_labels(FIRST)}})
    cluster.put(
        DEPLOYMENT,
        NAMESPACE,
        {"metadata": {"name": "prometheus", "labels": ownership_labels(FIRST)}},
    )

    claim(cluster, SECOND)


def test_find_active_runs_only_reads_owners(cluster: FakeResources) -> None:
    claim(cluster, FIRST)
    cluster.put(
        CONFIG_MAP,
        NAMESPACE,
        {"metadata": {"name": "definition", "labels": ownership_labels(SECOND)}},
    )

    active = find_active_runs(cluster, datetime.now(UTC))

    assert [run.run_id for run in active] == [str(FIRST)]
    assert active[0].owner == owner_name(FIRST)


# -- marking a run finished ------------------------------------------------------------


def test_mark_finished_records_when_the_run_ended(cluster: FakeResources) -> None:
    claim(cluster, FIRST)

    mark_finished(cluster, FIRST, namespace=NAMESPACE)

    assert FINISHED_AT_ANNOTATION in owner(cluster, FIRST)["metadata"]["annotations"]
    assert not claim_of(owner(cluster, FIRST)).is_active(datetime.now(UTC))


def test_a_run_that_keeps_its_evidence_says_why_on_its_owner(cluster: FakeResources) -> None:
    claim(cluster, FIRST)

    mark_finished(cluster, FIRST, namespace=NAMESPACE, retained="fail")

    finished = claim_of(owner(cluster, FIRST))
    assert finished.retained == "fail"
    # Kept evidence never holds the cluster.
    assert not finished.is_active(datetime.now(UTC))


def test_mark_finished_refuses_an_owner_that_is_not_this_runs(cluster: FakeResources) -> None:
    cluster.put(CONFIG_MAP, NAMESPACE, {"metadata": {"name": owner_name(FIRST)}})

    with pytest.raises(OwnershipError):
        mark_finished(cluster, FIRST, namespace=NAMESPACE)


# -- simultaneous starts -----------------------------------------------------------------


def _claim_simultaneously(cluster: FakeResources, run_ids: list[RunId]) -> dict[str, object]:
    barrier = threading.Barrier(len(run_ids))
    outcomes: dict[str, object] = {}

    def run(run_id: RunId) -> None:
        barrier.wait()
        try:
            outcomes[str(run_id)] = claim(cluster, run_id)
        except RunAlreadyActiveError as error:
            outcomes[str(run_id)] = error

    threads = [threading.Thread(target=run, args=(run_id,)) for run_id in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def _started_and_refused(
    outcomes: dict[str, object],
) -> tuple[list[str], list[RunAlreadyActiveError]]:
    started = [rid for rid, outcome in outcomes.items() if isinstance(outcome, RunOwnership)]
    refused = [o for o in outcomes.values() if isinstance(o, RunAlreadyActiveError)]
    return started, refused


@pytest.mark.parametrize("attempt", range(25))
def test_two_simultaneous_claims_start_exactly_one_run(attempt: int) -> None:
    outcomes = _claim_simultaneously(FakeResources(), [new_run_id(), new_run_id()])

    started, refused = _started_and_refused(outcomes)
    assert len(started) == 1
    assert len(refused) == 1
    assert refused[0].active_run_ids == started


@pytest.mark.parametrize("attempt", range(10))
def test_two_simultaneous_submissions_through_submit_start_exactly_one_run(
    attempt: int,
) -> None:
    """adrs#63: M1-04's submission and M1-05's claim together, not each in isolation.

    Both submissions create their owner, definition and Job first — exactly the objects
    that made both claims refuse each other before ADR-05-13 — and then both Jobs claim.
    """
    definition = parse_file(EXAMPLE)
    source = EXAMPLE.read_text(encoding="utf-8")
    fake = FakeCluster()
    run_ids = [new_run_id(), new_run_id()]
    for run_id in run_ids:
        submission = plan_submission(
            definition,
            source=source,
            source_path=EXAMPLE,
            image="img",
            images=RUN_IMAGES,
            cluster="test-cluster",
            run_id=str(run_id),
        )
        submit(fake, submission)

    started, refused = _started_and_refused(_claim_simultaneously(fake.store, run_ids))

    assert len(started) == 1
    assert len(refused) == 1

    # The refused submission's owner is left behind unclaimed, and blocks nobody: once
    # the started run finishes, a third run starts.
    mark_finished(fake.store, RunId(started[0]), namespace=NAMESPACE)
    claim(fake.store, new_run_id())


def test_the_lock_closes_the_window_between_check_and_claim(cluster: FakeResources) -> None:
    """The race the lock exists for, forced deterministically.

    FIRST is paused after taking the lock and before writing its claim — exactly where a
    check-only design would let SECOND see an empty cluster and start as well.
    """
    first_holds_lock = threading.Event()
    second_done = threading.Event()

    def pause_first(kind: Kind, namespace: str, body: Manifest) -> None:
        if kind == CONFIG_MAP and body["metadata"]["name"] == owner_name(FIRST):
            first_holds_lock.set()
            second_done.wait(timeout=5)

    cluster.before_replace = pause_first
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


# -- the lock itself ---------------------------------------------------------------------


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

    assert claim_of(owner(cluster, SECOND)).claimed_at is not None


def test_a_live_lock_refuses_even_with_nothing_else_in_the_cluster(
    cluster: FakeResources,
) -> None:
    _stale_lock(cluster, FIRST, age=timedelta(seconds=5), seconds=60)

    with pytest.raises(RunAlreadyActiveError, match=str(FIRST)):
        claim(cluster, SECOND)


def test_a_claim_that_loses_its_lock_midway_withdraws(cluster: FakeResources) -> None:
    """If the lock expired and changed hands while we were stalled, back out."""

    def take_over(kind: Kind, namespace: str, body: Manifest) -> None:
        annotations = body["metadata"].get("annotations") or {}
        if kind == CONFIG_MAP and CLAIMED_AT_ANNOTATION in annotations:
            lease = cluster.get(LEASE, LOCK_NAMESPACE, LOCK_NAME)
            lease["spec"]["holderIdentity"] = str(SECOND)
            cluster.before_replace = None
            cluster.replace(LEASE, LOCK_NAMESPACE, lease)

    cluster.before_replace = take_over

    with pytest.raises(RunAlreadyActiveError, match="taken over"):
        claim(cluster, FIRST)

    # The claim is withdrawn; the owner stays, since the Job and definition hang off it.
    assert claim_of(owner(cluster, FIRST)).claimed_at is None
    # The lock is someone else's now, so it is not ours to release.
    assert cluster.exists(LEASE, LOCK_NAMESPACE, LOCK_NAME)


def test_the_lock_is_released_when_the_claim_is_refused(cluster: FakeResources) -> None:
    claim(cluster, FIRST)
    with pytest.raises(RunAlreadyActiveError):
        claim(cluster, SECOND)

    assert not cluster.exists(LEASE, LOCK_NAMESPACE, LOCK_NAME)


def test_a_missing_lock_namespace_is_a_clear_error() -> None:
    cluster = FakeResources(namespaces={NAMESPACE})

    with pytest.raises(RunExclusivityError, match="'dokimi' does not exist") as broken:
        claim(cluster, FIRST)

    # Broken, not busy: only a refusal carries the run_already_active reason (ADR-01-11).
    assert not isinstance(broken.value, RunAlreadyActiveError)
    assert broken.value.reason is None


def test_the_lock_namespace_is_configurable(cluster: FakeResources) -> None:
    cluster.namespaces.add("tools")
    seen: list[str] = []

    def record(kind: Kind, namespace: str, body: Manifest) -> None:
        if kind == LEASE:
            seen.append(namespace)

    cluster.before_create = record

    claim(cluster, FIRST, lock_namespace="tools")

    assert seen == ["tools"]


def test_owners_are_selected_by_their_component(cluster: FakeResources) -> None:
    claim(cluster, FIRST)

    labels = owner(cluster, FIRST)["metadata"]["labels"]

    assert labels["app.kubernetes.io/component"] == OWNER_COMPONENT


# -- the claim's expiry (ADR-05-13) -------------------------------------------------------


def put_job(cluster: FakeResources, run_id: RunId, *, start_time: str | None) -> None:
    status = {} if start_time is None else {"startTime": start_time}
    cluster.put(
        JOB,
        NAMESPACE,
        {
            "metadata": {"name": resource_name(str(run_id)), "labels": ownership_labels(run_id)},
            "status": status,
        },
    )


def test_the_claim_expires_at_the_jobs_start_plus_its_deadline(cluster: FakeResources) -> None:
    # Not at "now" plus the deadline: phases 2 and 3 ran in between, and a stale claim
    # must not outlive the Job the cluster has already killed.
    put_job(cluster, FIRST, start_time="2026-09-24T14:30:15Z")

    expires = claim_expiry(cluster, FIRST, namespace=NAMESPACE, deadline_seconds=3600)

    assert expires == datetime(2026, 9, 24, 15, 30, 15, tzinfo=UTC)


@pytest.mark.parametrize("start_time", [None, "", "soon", "2026-09-24T14:30:15"])
def test_a_job_without_a_readable_start_time_cannot_be_claimed_for(
    cluster: FakeResources, start_time: str | None
) -> None:
    put_job(cluster, FIRST, start_time=start_time)

    with pytest.raises(RunExclusivityError, match="no readable start time"):
        claim_expiry(cluster, FIRST, namespace=NAMESPACE, deadline_seconds=3600)


def test_a_job_that_is_not_this_runs_is_not_trusted(cluster: FakeResources) -> None:
    cluster.put(
        JOB,
        NAMESPACE,
        {
            "metadata": {"name": resource_name(str(FIRST))},
            "status": {"startTime": "2026-09-24T14:30:15Z"},
        },
    )

    with pytest.raises(OwnershipError, match="not labelled as owned"):
        claim_expiry(cluster, FIRST, namespace=NAMESPACE, deadline_seconds=3600)
