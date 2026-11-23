"""Run exclusivity: at most one run active in a cluster, even when two start at once.

[ADR-05-02] preconditions every run on no other run being active anywhere in the cluster,
regardless of test type or namespace. [ADR-05-13] defines *active*: a run's owner carries
a claim that is not finished and not expired. [ADR-05-10] is the mechanism, and this
module is that mechanism.

The claim is made inside the Job, as the first check of phase 4 — never by the CLI, whose
credentials stay namespaced ([ADR-01-06]). What ``submit`` created in phase 1 (the owner,
the definition, the Job) carries no claim, so it is not evidence of an active run, and two
simultaneous submissions cannot refuse each other.

The mechanism, in :func:`claim_run`:

1. **Take the run-start lock** — a ``coordination.k8s.io/v1`` ``Lease`` with a fixed name
   in a fixed namespace, whose ``holderIdentity`` is the ``run_id``. ``create`` is atomic
   on the API server, so of two simultaneous claims exactly one gets it; the other is
   refused, naming the holder.
2. **Scan** every namespace for run owners whose claim is active. Any hit refuses the
   start, naming the run, its owner and when its claim expires.
3. **Claim** by writing ``claimed-at`` and ``claim-expires-at`` onto this run's own owner.
   From here on, the owner is what marks the run active to every later scan — the lock is
   not held for the run's duration, only for this check-and-claim window.
4. **Confirm** the lock is still ours, then release it. If it expired and was taken over
   while we were stalled, a second run may have scanned before our claim existed, so we
   withdraw our claim and refuse rather than risk two runs.

A lock left behind by a process that died inside that window expires after
``lock_seconds`` and is taken over with an optimistic-concurrency replace. A run that died
*after* claiming stops counting when its claim expires, which is when the cluster has
killed its Job; a run that ended normally stops counting as soon as :func:`mark_finished`
has run, at the start of phase 11, whether or not its evidence is retained.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dokimi.k8s.manifests import resource_name
from dokimi.k8s.ownership import (
    CLAIM_EXPIRES_AT_ANNOTATION,
    CLAIMED_AT_ANNOTATION,
    COMPONENT_LABEL,
    FINISHED_AT_ANNOTATION,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    OWNER_COMPONENT,
    RETAINED_ANNOTATION,
    RUN_ID_LABEL,
    OwnershipError,
    RunOwnership,
    claim_of,
    format_timestamp,
    is_run_owned,
    owner_name,
    ownership_labels,
)
from dokimi.k8s.resources import (
    CONFIG_MAP,
    JOB,
    LEASE,
    AlreadyExistsError,
    ClusterError,
    ConflictError,
    Manifest,
    NotFoundError,
    ResourceClient,
)
from dokimi.run_identity import RunId

__all__ = [
    "DEFAULT_LOCK_NAMESPACE",
    "DEFAULT_LOCK_SECONDS",
    "LOCK_COMPONENT",
    "LOCK_NAME",
    "REFUSAL_REASON",
    "ActiveRun",
    "RunAlreadyActiveError",
    "RunExclusivityError",
    "claim_expiry",
    "claim_run",
    "find_active_runs",
    "mark_finished",
]

_log = logging.getLogger(__name__)

#: Where the lock lives. An installation prerequisite (``deploy/rbac.yaml``); one lock
#: per cluster, whatever namespace a run uses ([ADR-05-10], OQ-108).
DEFAULT_LOCK_NAMESPACE = "dokimi"
LOCK_NAME = "dokimi-run-lock"
LOCK_COMPONENT = "run-lock"
#: Only has to cover one scan and one update, not the run.
DEFAULT_LOCK_SECONDS = 60
#: The reason a refused claim reports within the run-level preflight category
#: ([ADR-01-11]). A caller that retries "busy" but not "broken" tells them apart by it.
REFUSAL_REASON = "run_already_active"

_ACQUIRE_ATTEMPTS = 3
_WRITE_ATTEMPTS = 3
_MICROTIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
#: Every run owner, of any run. The claim is read from each; the selector only narrows.
_OWNER_SELECTOR = (
    f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{RUN_ID_LABEL},{COMPONENT_LABEL}={OWNER_COMPONENT}"
)


class RunExclusivityError(RuntimeError):
    """The exclusivity check itself could not be carried out.

    It carries no reason: only a refusal is "busy" ([ADR-01-11]). A missing lock
    namespace, a missing owner or retries running out mean the cluster is broken.
    """

    reason: str | None = None


@dataclass(frozen=True)
class ActiveRun:
    """Another run's owner, found carrying an active claim."""

    run_id: str
    namespace: str
    owner: str
    claimed_at: datetime | None
    expires_at: datetime | None

    def describe(self) -> str:
        expiry = format_timestamp(self.expires_at) if self.expires_at else "unknown"
        claimed = format_timestamp(self.claimed_at) if self.claimed_at else "unknown"
        return (
            f"ConfigMap {self.namespace}/{self.owner} claimed at {claimed}, "
            f"claim expires at {expiry}"
        )

    @property
    def cleanup_command(self) -> str:
        """Clears the run, although it still holds the cluster: hence ``--force``."""
        return f"dokimi cleanup {self.run_id} --namespace {self.namespace} --force"


class RunAlreadyActiveError(RunExclusivityError):
    """Another run is active, or is starting, in this cluster. Exits 4 ([ADR-01-11])."""

    reason = REFUSAL_REASON

    def __init__(
        self,
        run_id: RunId,
        active: Sequence[ActiveRun] = (),
        *,
        holder: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.active = list(active)
        ids = {run.run_id for run in self.active}
        if holder:
            ids.add(holder)
        self.active_run_ids = sorted(ids)

        names = ", ".join(self.active_run_ids) or "another run"
        noun = "run" if len(self.active_run_ids) <= 1 else "runs"
        lines = [
            f"refusing to start run {run_id} ({self.reason}): {noun} {names} already "
            "active in this cluster."
        ]
        if detail:
            lines.append(f"  - {detail}")
        lines.extend(f"  - {run.describe()}" for run in self.active)
        if self.active:
            lines.append(
                "Wait for it to finish, or for its claim to expire. If it is stale, because "
                "its Job is gone, clear it, which removes what it created with it:"
            )
            lines.extend(f"  {run.cleanup_command}" for run in self.active)
        else:
            lines.append("Retry once the other run has claimed the cluster or given up.")
        super().__init__("\n".join(lines))


def find_active_runs(
    cluster: ResourceClient,
    now: datetime,
    *,
    exclude: RunId | None = None,
    namespace: str | None = None,
) -> list[ActiveRun]:
    """Every run owner whose claim is active at *now*, in *namespace* or in every one.

    The labels are re-checked on each result, so a selector the server misread cannot
    smuggle in an object that is not ours.
    """
    found: list[ActiveRun] = []
    for item in cluster.list(CONFIG_MAP, _OWNER_SELECTOR, namespace):
        if not is_run_owned(item):
            continue
        metadata = item["metadata"]
        labels = metadata.get("labels") or {}
        if labels.get(COMPONENT_LABEL) != OWNER_COMPONENT:
            continue
        run_id = labels[RUN_ID_LABEL]
        if exclude is not None and run_id == str(exclude):
            continue
        claim = claim_of(item)
        if claim.is_active(now):
            found.append(
                ActiveRun(
                    run_id=run_id,
                    namespace=metadata["namespace"],
                    owner=metadata["name"],
                    claimed_at=claim.claimed_at,
                    expires_at=claim.expires_at,
                )
            )
    return sorted(found, key=lambda run: run.run_id)


def claim_expiry(
    cluster: ResourceClient,
    run_id: RunId,
    *,
    namespace: str,
    deadline_seconds: int,
) -> datetime:
    """When *run_id*'s claim expires: its Job's start time plus its deadline ([ADR-05-13]).

    The start time is read from the Job's status, so it is the API server's clock that
    dates the claim, and the claim expires exactly when the cluster kills the Job.
    *deadline_seconds* is the Job's ``activeDeadlineSeconds``, which ``submit`` passes to
    the Job alongside it.

    Raises:
        OwnershipError: the Job is not labelled as this run's.
        RunExclusivityError: the Job reports no readable start time.
        ClusterError: the Job cannot be read.
    """
    name = resource_name(str(run_id))
    job = cluster.get(JOB, namespace, name)
    if not is_run_owned(job, run_id):
        raise OwnershipError(
            f"Job {namespace}/{name} is not labelled as owned by run {run_id}; "
            "refusing to date a claim from it"
        )
    started = (job.get("status") or {}).get("startTime")
    try:
        started_at = datetime.fromisoformat(started) if started else None
    except ValueError:
        started_at = None
    if started_at is None or started_at.tzinfo is None:
        raise RunExclusivityError(
            f"Job {namespace}/{name} reports no readable start time ({started!r}), so its "
            "claim cannot be given an expiry: a claim ends with its Job's deadline, counted "
            "from that start"
        )
    return started_at.astimezone(UTC) + timedelta(seconds=deadline_seconds)


def claim_run(
    cluster: ResourceClient,
    run_id: RunId,
    *,
    namespace: str,
    expires_at: datetime,
    lock_namespace: str = DEFAULT_LOCK_NAMESPACE,
    metadata: Mapping[str, str] | None = None,
    lock_seconds: int = DEFAULT_LOCK_SECONDS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunOwnership:
    """Claim the cluster for *run_id*, or refuse. The first check of phase 4.

    *namespace* is where the run's owner lives — the namespace the Job runs in. ``submit``
    created the owner there; the claim never creates one, and the Job's role could not.
    *expires_at* is the orchestrator Job's own deadline ([ADR-05-13]), from
    :func:`claim_expiry`.

    Returns the :class:`RunOwnership` through which every later object must be created.

    Raises:
        RunAlreadyActiveError: another run is active or is starting.
        RunExclusivityError: the lock could not be taken or confirmed, or the run's owner
            does not exist.
    """
    lease = _acquire_lock(cluster, run_id, lock_namespace, lock_seconds, now)
    try:
        active = find_active_runs(cluster, now(), exclude=run_id)
        if active:
            raise RunAlreadyActiveError(run_id, active)

        ownership = RunOwnership(cluster, run_id, metadata=metadata)
        owner = ownership.existing_owner_in(namespace)
        claimed = _annotate(
            cluster,
            owner,
            {
                CLAIMED_AT_ANNOTATION: format_timestamp(now()),
                CLAIM_EXPIRES_AT_ANNOTATION: format_timestamp(expires_at),
            },
            remove=(FINISHED_AT_ANNOTATION, RETAINED_ANNOTATION),
        )
        ownership.remember(claimed)

        if not _still_holds(cluster, lease, lock_namespace):
            _withdraw(cluster, claimed)
            raise RunAlreadyActiveError(
                run_id,
                holder=_current_holder(cluster, lock_namespace),
                detail="the run-start lock expired while this run was claiming and was taken over",
            )
        return ownership
    finally:
        _release_lock(cluster, lease, lock_namespace)


def mark_finished(
    cluster: ResourceClient,
    run_id: RunId,
    *,
    namespace: str,
    retained: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Manifest:
    """Record that *run_id* has finished, and, with *retained*, why it keeps its evidence.

    Retained evidence is for inspection, and must not hold the cluster. Raises
    :class:`ClusterError` if the owner cannot be updated; the run's claim then still ends
    when it expires.
    """
    owner = cluster.get(CONFIG_MAP, namespace, owner_name(run_id))
    if not is_run_owned(owner, run_id):
        raise OwnershipError(
            f"ConfigMap {namespace}/{owner_name(run_id)} is not labelled as owned by run "
            f"{run_id}; refusing to mark it finished"
        )
    annotations = {FINISHED_AT_ANNOTATION: format_timestamp(now())}
    if retained is not None:
        annotations[RETAINED_ANNOTATION] = retained
    return _annotate(cluster, owner, annotations)


def _annotate(
    cluster: ResourceClient,
    owner: Manifest,
    annotations: Mapping[str, str],
    *,
    remove: Sequence[str] = (),
) -> Manifest:
    """Set *annotations* on *owner* with an optimistic-concurrency replace."""
    metadata = owner["metadata"]
    namespace, name = metadata["namespace"], metadata["name"]
    current = owner
    for _ in range(_WRITE_ATTEMPTS):
        body = copy.deepcopy(current)
        values = body["metadata"].setdefault("annotations", {}) or {}
        body["metadata"]["annotations"] = values
        for key in remove:
            values.pop(key, None)
        values.update(annotations)
        try:
            return cluster.replace(CONFIG_MAP, namespace, body)
        except ConflictError:
            current = cluster.get(CONFIG_MAP, namespace, name)
            if current["metadata"]["uid"] != metadata["uid"]:
                raise OwnershipError(
                    f"ConfigMap {namespace}/{name} was replaced by a different object"
                ) from None
    raise RunExclusivityError(
        f"could not update ConfigMap {namespace}/{name} after {_WRITE_ATTEMPTS} attempts; "
        "it is changing too fast. Retry the run."
    )


def _acquire_lock(
    cluster: ResourceClient,
    run_id: RunId,
    lock_namespace: str,
    lock_seconds: int,
    now: Callable[[], datetime],
) -> Manifest:
    for _ in range(_ACQUIRE_ATTEMPTS):
        body = _lease_manifest(run_id, lock_namespace, lock_seconds, now())
        try:
            return cluster.create(LEASE, lock_namespace, body)
        except AlreadyExistsError:
            pass
        except NotFoundError as error:
            raise RunExclusivityError(
                f"cannot take the run-start lock: namespace {lock_namespace!r} does not exist. "
                "It is an installation prerequisite; apply deploy/rbac.yaml"
            ) from error

        try:
            current = cluster.get(LEASE, lock_namespace, LOCK_NAME)
        except NotFoundError:
            continue  # released between our create and our read; try again

        holder = current.get("spec", {}).get("holderIdentity") or "<unknown>"
        if not _expired(current, now()):
            raise RunAlreadyActiveError(
                run_id,
                holder=holder,
                detail=f"it holds the run-start lock {lock_namespace}/{LOCK_NAME}",
            )

        _log.warning("taking over the expired run-start lock left by run %s", holder)
        body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        try:
            return cluster.replace(LEASE, lock_namespace, body)
        except ConflictError, NotFoundError:
            continue  # someone else took it over, or released it, first; look again

    raise RunExclusivityError(
        f"could not take the run-start lock {lock_namespace}/{LOCK_NAME} after "
        f"{_ACQUIRE_ATTEMPTS} attempts; it is changing hands too fast. Retry the run."
    )


def _lease_manifest(
    run_id: RunId, lock_namespace: str, lock_seconds: int, moment: datetime
) -> Manifest:
    stamp = moment.astimezone(UTC).strftime(_MICROTIME_FORMAT)
    return {
        "apiVersion": LEASE.api_version,
        "kind": LEASE.kind,
        "metadata": {
            "name": LOCK_NAME,
            "namespace": lock_namespace,
            "labels": ownership_labels(run_id, component=LOCK_COMPONENT),
        },
        "spec": {
            "holderIdentity": str(run_id),
            "leaseDurationSeconds": lock_seconds,
            "acquireTime": stamp,
            "renewTime": stamp,
        },
    }


def _expired(lease: Manifest, moment: datetime) -> bool:
    spec = lease.get("spec", {})
    renewed = spec.get("renewTime") or spec.get("acquireTime")
    duration = spec.get("leaseDurationSeconds")
    if not renewed or duration is None:
        return True  # a lock that does not say how long it lasts cannot be trusted to
    renewed_at = datetime.fromisoformat(str(renewed).replace("Z", "+00:00"))
    return moment >= renewed_at + timedelta(seconds=int(duration))


def _still_holds(cluster: ResourceClient, lease: Manifest, lock_namespace: str) -> bool:
    try:
        current = cluster.get(LEASE, lock_namespace, LOCK_NAME)
    except NotFoundError:
        return False
    return bool(current["metadata"]["uid"] == lease["metadata"]["uid"]) and (
        current["metadata"].get("resourceVersion") == lease["metadata"].get("resourceVersion")
    )


def _current_holder(cluster: ResourceClient, lock_namespace: str) -> str | None:
    try:
        current = cluster.get(LEASE, lock_namespace, LOCK_NAME)
    except NotFoundError:
        return None
    holder = current.get("spec", {}).get("holderIdentity")
    return str(holder) if holder else None


def _withdraw(cluster: ResourceClient, owner: Manifest) -> None:
    """Remove this run's claim, keeping the owner: the Job and definition still hang off it."""
    try:
        _annotate(cluster, owner, {}, remove=(CLAIMED_AT_ANNOTATION, CLAIM_EXPIRES_AT_ANNOTATION))
    except ClusterError as error:
        _log.error(
            "could not withdraw the claim on %s/%s; it stops counting when it expires: %s",
            owner["metadata"]["namespace"],
            owner["metadata"]["name"],
            error,
        )


def _release_lock(cluster: ResourceClient, lease: Manifest, lock_namespace: str) -> None:
    try:
        # A takeover replaces the Lease in place, keeping its uid, so the uid alone would
        # not stop us from releasing a lock that is now someone else's.
        cluster.delete(
            LEASE,
            lock_namespace,
            LOCK_NAME,
            uid=lease["metadata"]["uid"],
            resource_version=lease["metadata"].get("resourceVersion"),
        )
    except NotFoundError, ConflictError:
        pass  # expired and taken over; it is no longer ours to release
    except ClusterError as error:
        # Never mask the claim's own outcome. The lock expires on its own.
        _log.warning("could not release the run-start lock; it expires by itself: %s", error)
