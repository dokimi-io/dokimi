"""Run exclusivity: at most one run active in a cluster, even when two start at once.

[ADR-05-02] preconditions every run on no other run being active anywhere in the cluster,
regardless of test type or namespace, and checks that by looking for run-owned objects
across all namespaces. A check alone has a race: two submissions that start at nearly the
same time can both look, both find nothing, and both start. ADR-05-02 names a lease or lock
object as the expected fix; this module is that fix.

The mechanism, in :func:`claim_run`:

1. **Take the run-start lock** — a ``coordination.k8s.io/v1`` ``Lease`` with a fixed name
   in a fixed namespace, whose ``holderIdentity`` is the ``run_id``. ``create`` is atomic
   on the API server, so of two simultaneous submissions exactly one gets it; the other
   is refused, naming the holder.
2. **Scan** every namespace for objects owned by any other run. Any hit refuses the start,
   naming the run(s) found.
3. **Claim** by creating this run's placeholder owner. From here on, the owner's existence
   is what marks the run active to every later scan — the lock is not held for the run's
   duration, only for this check-and-claim window.
4. **Confirm** the lock is still ours, then release it. If it expired and was taken over
   while we were stalled, a second run may have scanned before our owner existed, so we
   withdraw our owner and refuse rather than risk two runs.

A lock left behind by a process that died inside that window expires after
``lock_seconds`` and is taken over with an optimistic-concurrency replace, so a crash can
delay the next run by at most that long. A run that died *after* claiming leaves its
owner behind, and that refuses later runs until it is cleaned up — by design, since its
workloads may still be running, and its resources are retained on failure as evidence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from test_orchestrator.k8s.cluster import (
    CONFIG_MAP,
    LEASE,
    AlreadyExistsError,
    ClusterClient,
    ClusterError,
    ConflictError,
    Manifest,
    NotFoundError,
)
from test_orchestrator.k8s.ownership import (
    OwnedObject,
    RunOwnership,
    find_run_owned,
    owner_name,
    ownership_labels,
    run_selector,
)
from test_orchestrator.run_identity import RunId

__all__ = [
    "DEFAULT_LOCK_NAMESPACE",
    "DEFAULT_LOCK_SECONDS",
    "LOCK_COMPONENT",
    "LOCK_NAME",
    "RunAlreadyActiveError",
    "RunExclusivityError",
    "claim_run",
]

_log = logging.getLogger(__name__)

#: Where the lock lives. Must exist before a run starts; overridable per call.
DEFAULT_LOCK_NAMESPACE = "test-orchestrator"
LOCK_NAME = "test-orchestrator-run-lock"
LOCK_COMPONENT = "run-lock"
#: Only has to cover one scan and one create, not the run.
DEFAULT_LOCK_SECONDS = 60

_ACQUIRE_ATTEMPTS = 3
_MICROTIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class RunExclusivityError(RuntimeError):
    """The exclusivity check itself could not be carried out."""


class RunAlreadyActiveError(RunExclusivityError):
    """Another run is active, or is starting, in this cluster."""

    def __init__(self, run_id: RunId, active_run_ids: Sequence[str], evidence: Sequence[str]):
        self.run_id = run_id
        self.active_run_ids = sorted(set(active_run_ids))
        self.evidence = list(evidence)
        names = ", ".join(self.active_run_ids)
        noun = "run" if len(self.active_run_ids) == 1 else "runs"
        lines = [
            f"refusing to start run {run_id}: {noun} {names} already active in this cluster.",
            *(f"  - {item}" for item in self.evidence),
            "Wait for it to finish. If it is stale, its resources are selected by:",
            *(f"  {run_selector(RunId(active))}" for active in self.active_run_ids),
        ]
        super().__init__("\n".join(lines))


def claim_run(
    cluster: ClusterClient,
    run_id: RunId,
    *,
    namespace: str,
    lock_namespace: str = DEFAULT_LOCK_NAMESPACE,
    metadata: Mapping[str, str] | None = None,
    lock_seconds: int = DEFAULT_LOCK_SECONDS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunOwnership:
    """Claim the cluster for *run_id*, or refuse. Call before creating anything else.

    *namespace* is where the run's first owner is created — the test type's namespace.
    Returns the :class:`RunOwnership` through which every later object must be created.

    Raises:
        RunAlreadyActiveError: another run is active or is starting.
        RunExclusivityError: the lock could not be taken or confirmed.
    """
    lease = _acquire_lock(cluster, run_id, lock_namespace, lock_seconds, now)
    try:
        others = [obj for obj in find_run_owned(cluster) if obj.run_id != str(run_id)]
        if others:
            raise RunAlreadyActiveError(run_id, [obj.run_id for obj in others], _evidence(others))

        ownership = RunOwnership(cluster, run_id, metadata=metadata)
        ownership.owner_in(namespace)

        if not _still_holds(cluster, lease, lock_namespace):
            _withdraw(cluster, ownership, namespace)
            holder = _current_holder(cluster, lock_namespace)
            raise RunAlreadyActiveError(
                run_id,
                [holder] if holder else [],
                ["the run-start lock expired while this run was claiming and was taken over"],
            )
        return ownership
    finally:
        _release_lock(cluster, lease, lock_namespace)


def _acquire_lock(
    cluster: ClusterClient,
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
                f"cannot take the run-start lock: namespace {lock_namespace!r} does not exist"
            ) from error

        try:
            current = cluster.get(LEASE, lock_namespace, LOCK_NAME)
        except NotFoundError:
            continue  # released between our create and our read; try again

        holder = current.get("spec", {}).get("holderIdentity") or "<unknown>"
        if not _expired(current, now()):
            raise RunAlreadyActiveError(
                run_id, [holder], [f"it holds the run-start lock {lock_namespace}/{LOCK_NAME}"]
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


def _still_holds(cluster: ClusterClient, lease: Manifest, lock_namespace: str) -> bool:
    try:
        current = cluster.get(LEASE, lock_namespace, LOCK_NAME)
    except NotFoundError:
        return False
    return bool(current["metadata"]["uid"] == lease["metadata"]["uid"]) and (
        current["metadata"].get("resourceVersion") == lease["metadata"].get("resourceVersion")
    )


def _current_holder(cluster: ClusterClient, lock_namespace: str) -> str | None:
    try:
        current = cluster.get(LEASE, lock_namespace, LOCK_NAME)
    except NotFoundError:
        return None
    holder = current.get("spec", {}).get("holderIdentity")
    return str(holder) if holder else None


def _withdraw(cluster: ClusterClient, ownership: RunOwnership, namespace: str) -> None:
    owner = ownership.owner_in(namespace)
    try:
        cluster.delete(
            CONFIG_MAP, namespace, owner_name(ownership.run_id), uid=owner["metadata"]["uid"]
        )
    except ClusterError as error:
        _log.error("could not withdraw the owner of run %s: %s", ownership.run_id, error)


def _release_lock(cluster: ClusterClient, lease: Manifest, lock_namespace: str) -> None:
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


def _evidence(objects: Sequence[OwnedObject], limit: int = 10) -> list[str]:
    lines = [f"{obj} belongs to run {obj.run_id}" for obj in objects[:limit]]
    if len(objects) > limit:
        lines.append(f"... and {len(objects) - limit} more")
    return lines
