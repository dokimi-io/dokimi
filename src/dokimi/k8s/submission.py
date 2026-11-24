"""Phase 1: submit the run, follow it, and find it again afterwards.

The whole of ADR-01-06's client-side contract lives here. Three properties it asks for,
and where each one comes from:

* **The Job is unaffected when the submitter disconnects.** Nothing in this module holds
  the run together. Following a run is a read of a server-side resource; stopping the
  read stops nothing.
* **Reattaching uses the run's ownership labels, not the submitting process's state.**
  :func:`follow` takes a ``run_id`` and a namespace and needs nothing else, which is why
  the same function serves ``submit`` and ``attach`` — and why a reattach works from a
  different machine.
* **A dry run touches nothing.** Rendering is :func:`plan_submission`, a pure function
  that never sees a client.

The run's exit code is the container's exit code, unchanged. ADR-01-08 makes the Job's
exit status the verdict, so translating it here would be inventing a second opinion.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dokimi.k8s.cluster import ClusterClient, ClusterError
from dokimi.k8s.identity import (
    new_run_id,
    run_selector,
    validate_run_id,
)
from dokimi.k8s.manifests import (
    RUN_COMPONENT,
    bind_owner,
    render_manifests,
    resource_name,
)
from dokimi.k8s.namespaces import namespace_for
from dokimi.k8s.ownership import delete_run_owned
from dokimi.k8s.resources import CONFIG_MAP, JOB
from dokimi.model import TestDefinition
from dokimi.run_identity import RunId
from dokimi.testtypes.interface import WorkloadImage

if TYPE_CHECKING:
    # Only a type here: the cleanup package sits above this module.
    from dokimi.cleanup.retention import Retention

__all__ = [
    "CONTAINER_NAME",
    "RUN_OUTCOMES",
    "RunNotFound",
    "Submission",
    "find_job",
    "follow",
    "plan_submission",
    "submit",
]

CONTAINER_NAME = "orchestrator"

#: How long to wait for the API server to schedule a pod for the Job before giving up.
#: Long enough to cover an image pull on a cold node; short enough that a run wedged in
#: `Pending` because nothing can schedule it does not hang a CI job indefinitely.
POD_APPEARANCE_TIMEOUT_SECONDS = 300.0


#: The exit codes that are a run's outcome — ADR-01-08's table, 0 to 5. The CLI's
#: ``ExitCode`` enumerates the same values; ``tests/test_submission.py`` holds the two to
#: each other. Anything else a container exits with is a signal or a crash, not a verdict.
RUN_OUTCOMES = frozenset(range(6))

#: What the common out-of-table codes usually mean, for the message that reports them.
_SIGNAL_MEANINGS = {
    137: "killed with SIGKILL, most often because it ran out of memory (OOMKilled)",
    143: "terminated with SIGTERM, usually because the pod was deleted or its node drained",
}


class RunNotFound(LookupError):
    """No Job carries the requested run's ownership labels."""


@dataclass(frozen=True, slots=True)
class Submission:
    """Everything a submission consists of, before any of it exists in a cluster."""

    run_id: str
    namespace: str
    image: str
    #: The images the run deploys, by component (ADR-04-08).
    images: Mapping[str, WorkloadImage]
    #: What the operator called the cluster, for every result's ``cluster`` label (ADR-02-07).
    cluster: str
    manifests: tuple[dict[str, Any], ...]

    @property
    def job_name(self) -> str:
        return resource_name(self.run_id)


def plan_submission(
    definition: TestDefinition,
    *,
    source: str,
    source_path: Path,
    image: str,
    images: Mapping[str, WorkloadImage],
    cluster: str,
    run_id: str | None = None,
    namespace: str | None = None,
    service_account: str | None = None,
    preflight_only: bool = False,
    retention: Retention | None = None,
) -> Submission:
    """Render everything a run needs, without contacting anything.

    This is the whole of ``--dry-run``: what it prints is what ``submit`` would create,
    produced by the same call, so the two cannot drift.
    """
    # Validated here rather than only in `attach`: the id becomes a label value, part of
    # every object name and a term in the selectors that find the run again, so a bad one
    # breaks all three — and a dry run would print manifests that cannot be applied.
    resolved_run_id = validate_run_id(run_id) if run_id is not None else new_run_id()
    # Validated like the default: a malformed --namespace is a config_error (ADR-01-11),
    # not an API server refusal after the owner has already been created.
    resolved_namespace = namespace_for(definition.test_type.value, namespace)
    manifests = render_manifests(
        definition,
        source=source,
        source_suffix=source_path.suffix,
        run_id=resolved_run_id,
        image=image,
        namespace=resolved_namespace,
        images=images,
        cluster=cluster,
        service_account=service_account,
        preflight_only=preflight_only,
        retention=retention,
    )
    return Submission(
        run_id=resolved_run_id,
        namespace=resolved_namespace,
        image=image,
        images=dict(images),
        cluster=cluster,
        manifests=tuple(manifests),
    )


def submit(cluster: ClusterClient, submission: Submission) -> None:
    """Create the run's objects, owner first.

    The owner's uid is only known once the API server has assigned it, so the remaining
    manifests are bound to it here rather than at render time. Until that binding
    happens there is nothing for the garbage collector to cascade from, which is why the
    owner is created first and alone.

    The namespace must already exist: a run never creates one (ADR-05-13).
    """
    owner, *rest = submission.manifests
    created_owner = cluster.create(owner)
    owner_uid = created_owner.get("metadata", {}).get("uid", "")
    if not owner_uid:
        raise ClusterError(
            "the API server returned the run's owner object without a uid, so nothing "
            "can be bound to it for garbage collection"
        )

    try:
        for manifest in rest:
            cluster.create(bind_owner(manifest, owner_uid))
    except ClusterError as error:
        _abandon(cluster, submission, error)
        raise


def _abandon(cluster: ClusterClient, submission: Submission, error: ClusterError) -> None:
    """Remove a submission that failed after its owner was created.

    This is not ADR-05-02's retain-on-failure, which keeps a failed *run's* evidence: a
    Job that was never created left no evidence, only an unclaimed owner. That owner
    would not block later runs (ADR-05-13), but it is debris all the same.

    :func:`~dokimi.k8s.ownership.delete_run_owned` re-checks the labels on what it
    selected and deletes each object by name and uid, so a broken selector cannot widen
    what a failed submission removes.
    """
    selector = run_selector(submission.run_id)
    remedy = f"kubectl delete configmap,job -n {submission.namespace} -l {selector}"
    try:
        report = delete_run_owned(
            cluster.resources(),
            RunId(submission.run_id),
            namespace=submission.namespace,
            kinds=(CONFIG_MAP, JOB),
        )
    except ClusterError as cleanup_error:
        # ADR-05-02: cleanup failures are reported with what remains and how to remove
        # it, never swallowed. The original error is still the one that is raised.
        error.add_note(
            f"Removing the partial submission failed too ({cleanup_error}). Remove it with: "
            f"{remedy}"
        )
        return
    if not report.complete:
        remaining = ", ".join(str(obj) for obj, _ in report.failed)
        error.add_note(
            f"Removing the partial submission left {remaining} behind. Remove it with: {remedy}"
        )


def follow(
    cluster: ClusterClient,
    *,
    namespace: str,
    run_id: str,
    write: Callable[[str], None],
    timeout: float = POD_APPEARANCE_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Stream a run's logs until it ends, and return the run's exit code.

    Locating the pod by label selector rather than by the name ``submit`` happened to
    create is what makes this usable from a process that never submitted anything.

    The code returned is always one of :data:`RUN_OUTCOMES`. A container that exits with
    anything else — a signal, a crash, a deadline — raises :class:`ClusterError` saying
    which, because passing 137 through would hand CI a number that means nothing in
    ADR-01-08's table and everything to a shell.
    """
    pod = _await_pod(cluster, namespace, run_id, timeout=timeout, sleep=sleep, clock=clock)
    pod_name = pod["metadata"]["name"]

    try:
        for line in cluster.stream_logs(namespace, pod_name, CONTAINER_NAME):
            write(line)
    except ClusterError as error:
        error.add_note(f"The run is unaffected; `dokimi attach {run_id}` resumes following it.")
        raise

    code, reason = _await_exit_code(cluster, namespace, run_id, pod_name, sleep=sleep, clock=clock)
    if code in RUN_OUTCOMES:
        return code
    if _deadline_exceeded(cluster, namespace, run_id):
        raise ClusterError(_deadline_message(run_id))
    meaning = _SIGNAL_MEANINGS.get(code) or (
        f"killed by signal {code - 128}" if code > 128 else "a code outside the outcome table"
    )
    detail = f", reason {reason}" if reason else ""
    raise ClusterError(
        f"run {run_id!r} exited with {code}{detail}: {meaning}. That is not a run outcome, "
        "so the run ended without a verdict."
    )


def find_job(cluster: ClusterClient, namespace: str, run_id: str) -> dict[str, Any] | None:
    """The run's Job, located by its ownership labels."""
    return cluster.find_job(namespace, run_selector(run_id, RUN_COMPONENT))


def _await_pod(
    cluster: ClusterClient,
    namespace: str,
    run_id: str,
    *,
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> dict[str, Any]:
    """Wait for the run's pod to exist and to have started.

    A pod's logs cannot be read while it is still `Pending`, and the pod itself does not
    exist for a moment after the Job does.
    """
    selector = run_selector(run_id, RUN_COMPONENT)
    deadline = clock() + timeout
    seen_job = False
    while True:
        pod = cluster.find_pod(namespace, selector)
        if pod is not None and _phase(pod) != "Pending":
            return pod
        if pod is None and not seen_job:
            if find_job(cluster, namespace, run_id) is None:
                raise RunNotFound(
                    f"no run {run_id!r} in namespace {namespace!r}. A run's Job is removed "
                    "once its artifacts are fetched, or an hour after it ended, so it may be "
                    "gone already, or it may be in another namespace."
                )
            seen_job = True
        if clock() >= deadline:
            state = "not scheduled" if pod is None else f"still {_phase(pod)}"
            raise ClusterError(
                f"run {run_id!r} is {state} after {timeout:.0f}s. The run itself is "
                f"unaffected; `dokimi attach {run_id}` picks it up again."
            )
        sleep(1.0)


def _await_exit_code(
    cluster: ClusterClient,
    namespace: str,
    run_id: str,
    pod_name: str,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout: float = 60.0,
) -> tuple[int, str]:
    """Read the container's exit code and reason once the log stream has ended.

    The stream closing and the status reporting a terminated container are two different
    events, and they are not simultaneous.
    """
    deadline = clock() + timeout
    while True:
        pod = cluster.read_pod(namespace, pod_name)
        if pod is None:
            # The deadline lives on the Job, and when it passes the Job controller deletes
            # the pod. So a vanished pod is the usual sign of a deadline, and the Job's
            # own conditions are where that is recorded.
            if _deadline_exceeded(cluster, namespace, run_id):
                raise ClusterError(_deadline_message(run_id))
            raise ClusterError(
                f"pod {pod_name!r} disappeared before it reported an exit code, so the "
                "run's outcome cannot be read from the cluster."
            )
        terminated = _terminated(pod)
        if terminated is not None:
            return terminated
        if clock() >= deadline:
            raise ClusterError(
                f"pod {pod_name!r} finished streaming but never reported an exit code."
            )
        sleep(1.0)


def _deadline_exceeded(cluster: ClusterClient, namespace: str, run_id: str) -> bool:
    job = find_job(cluster, namespace, run_id)
    conditions = (job or {}).get("status", {}).get("conditions") or []
    return any(
        condition.get("type") == "Failed"
        and condition.get("status") == "True"
        and condition.get("reason") == "DeadlineExceeded"
        for condition in conditions
    )


def _deadline_message(run_id: str) -> str:
    return (
        f"run {run_id!r} hit its activeDeadlineSeconds and was stopped before it could "
        "report an outcome. The deadline exists so an orphaned run stops on its own; "
        "a run that needs longer than its test duration plus the margin is "
        "stuck somewhere, and its pods and events are the place to look."
    )


def _phase(pod: dict[str, Any]) -> str:
    phase: str = pod.get("status", {}).get("phase", "Pending")
    return phase


def _terminated(pod: dict[str, Any]) -> tuple[int, str] | None:
    """The orchestrator container's exit code and reason, once it has terminated."""
    for status in pod.get("status", {}).get("containerStatuses", []) or []:
        if status.get("name") != CONTAINER_NAME:
            continue
        terminated = status.get("state", {}).get("terminated")
        if terminated is not None and "exitCode" in terminated:
            return int(terminated["exitCode"]), str(terminated.get("reason") or "")
    return None
