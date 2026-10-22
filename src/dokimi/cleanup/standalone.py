"""Removing a run after it ended, from outside it: ``dokimi cleanup``, and after a fetch.

Nothing here needs the run's own process or anything it remembered. A run is found by the
labels on what it created, so a run whose Job crashed, or whose submitter lost its
connection, can be removed just the same. That is what makes recovery possible.

Two callers:

* ``dokimi cleanup`` removes one run, or every run older than an age. A run that still
  holds the cluster is refused unless forced, because its Job may still be running it.
  A kept Prometheus stays unless asked for.
* ``submit`` and ``attach`` release a run once they have fetched its artifacts. A run that
  finished and kept nothing goes completely; one that kept its evidence, or never finished
  cleanly, loses only its Job, and the rest waits for ``dokimi cleanup``.
"""

from __future__ import annotations

import contextlib
import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from dokimi.k8s.identity import run_selector as exact_run_selector
from dokimi.k8s.ownership import (
    ORCHESTRATOR_COMPONENT,
    PROMETHEUS_COMPONENT,
    DeletionReport,
    OwnedObject,
    claim_of,
    delete_objects,
    find_run_owned,
    format_timestamp,
    is_run_owned,
    owner_name,
)
from dokimi.k8s.resources import (
    CONFIG_MAP,
    JOB,
    ConflictError,
    Kind,
    Manifest,
    NotFoundError,
    ResourceClient,
)
from dokimi.run_identity import InvalidRunIdError, RunId, parse_run_id

__all__ = [
    "Release",
    "RunCleanup",
    "clean_up_run",
    "release_fetched_run",
    "runs_older_than",
]


@dataclass
class RunCleanup:
    """What :func:`clean_up_run` did, or with a dry run, would do."""

    run_id: RunId
    #: Why nothing was removed: the run still holds the cluster.
    refused: str | None = None
    #: What goes, or with a dry run, would go.
    removable: list[OwnedObject] = field(default_factory=list)
    #: What stays on purpose: a kept Prometheus without ``--prometheus``.
    kept: list[OwnedObject] = field(default_factory=list)
    #: ``None`` for a dry run, or for one that was refused.
    deletion: DeletionReport | None = None

    @property
    def complete(self) -> bool:
        return self.refused is None and (self.deletion is None or self.deletion.complete)


def clean_up_run(
    resources: ResourceClient,
    run_id: RunId,
    namespace: str,
    *,
    remove_prometheus: bool = False,
    force: bool = False,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunCleanup:
    """Remove everything *run_id* created in *namespace*: by its labels, each by uid.

    The run's Job is not deleted directly. It belongs to the run's owner, which goes last,
    so the cluster's garbage collector removes the Job, and its pod, after everything
    this function deleted itself.
    """
    result = RunCleanup(run_id)
    owner = _owner(resources, run_id, namespace)
    claim = None if owner is None else claim_of(owner)
    if claim is not None and claim.is_active(now()) and not force:
        until = "" if claim.expires_at is None else f" until {format_timestamp(claim.expires_at)}"
        result.refused = (
            f"run {run_id} still holds the cluster{until}, so its Job may still be running "
            "it. Wait for it to finish, or, if its Job is gone, add --force"
        )
        return result
    # A controller's objects go with it, and a terminating one is going already: listing
    # either as removed would be untrue, and deleting a Deployment's pod only replaces it.
    objects = [
        obj
        for obj in find_run_owned(resources, run_id, namespace=namespace)
        if not (obj.controlled or obj.terminating)
    ]
    for obj in objects:
        kept = obj.component == PROMETHEUS_COMPONENT and not remove_prometheus
        (result.kept if kept else result.removable).append(obj)
    if not dry_run:
        result.deletion = delete_objects(resources, run_id, result.removable)
    return result


def runs_older_than(
    resources: ResourceClient,
    namespace: str,
    age: timedelta,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[RunId]:
    """Every run with anything left in *namespace* that started more than *age* ago.

    A run's age is read from its id, which is its start time. Every run-owned object
    counts, not only owners: a crashed run can leave objects whose owner is already gone.
    """
    cutoff = now() - age
    found: set[RunId] = set()
    for obj in find_run_owned(resources, namespace=namespace):
        try:
            run_id = parse_run_id(obj.run_id)
        except InvalidRunIdError:
            continue
        if run_id.started_at < cutoff:
            found.add(run_id)
    return sorted(found)


class Release(enum.Enum):
    """What :func:`release_fetched_run` removed."""

    #: The run kept nothing, so its owner went, and everything else of it with that.
    RUN = "run"
    #: The run keeps its evidence, or did not finish cleanly: only its Job went.
    JOB = "job"


def release_fetched_run(resources: ResourceClient, run_id: RunId, namespace: str) -> Release:
    """Remove what a run no longer needs once its artifacts are on the submitter's side.

    Its owner goes, and the garbage collector takes the Job and the definition with it, if
    the run finished and kept nothing, or never got as far as claiming the cluster. Any
    other run loses only its Job: one that kept its evidence, and one whose claim is not
    marked finished, because then its test pods may still exist. Raises
    :class:`~dokimi.k8s.resources.ClusterError` if a delete is refused.
    """
    owner = _owner(resources, run_id, namespace)
    if owner is not None:
        claim = claim_of(owner)
        finished = claim.claimed_at is None or claim.finished_at is not None
        if finished and claim.retained is None:
            metadata = owner["metadata"]
            _delete(resources, CONFIG_MAP, namespace, metadata["name"], metadata["uid"])
            return Release.RUN
    selector = exact_run_selector(str(run_id), ORCHESTRATOR_COMPONENT)
    for job in resources.list(JOB, selector, namespace):
        metadata = job["metadata"]
        _delete(resources, JOB, namespace, metadata["name"], metadata["uid"])
    return Release.JOB


def _owner(resources: ResourceClient, run_id: RunId, namespace: str) -> Manifest | None:
    """The run's owner, if it exists and is labelled as the run's: nothing else is read."""
    try:
        owner = resources.get(CONFIG_MAP, namespace, owner_name(run_id))
    except NotFoundError:
        return None
    return owner if is_run_owned(owner, run_id) else None


def _delete(resources: ResourceClient, kind: Kind, namespace: str, name: str, uid: str) -> None:
    """Delete by uid. One already gone, or replaced by another object, is not ours."""
    with contextlib.suppress(NotFoundError, ConflictError):
        resources.delete(kind, namespace, name, uid=uid)
