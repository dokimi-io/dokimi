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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from test_orchestrator.k8s.cluster import ClusterClient, ClusterError
from test_orchestrator.k8s.identity import default_namespace, new_run_id, run_selector
from test_orchestrator.k8s.manifests import (
    RUN_COMPONENT,
    bind_owner,
    namespace_manifest,
    render_manifests,
    resource_name,
)
from test_orchestrator.model import TestDefinition

__all__ = [
    "CONTAINER_NAME",
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


class RunNotFound(LookupError):
    """No Job carries the requested run's ownership labels."""


@dataclass(frozen=True, slots=True)
class Submission:
    """Everything a submission consists of, before any of it exists in a cluster."""

    run_id: str
    namespace: str
    image: str
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
    run_id: str | None = None,
    namespace: str | None = None,
    service_account: str | None = None,
) -> Submission:
    """Render everything a run needs, without contacting anything.

    This is the whole of ``--dry-run``: what it prints is what ``submit`` would create,
    produced by the same call, so the two cannot drift.
    """
    resolved_run_id = run_id or new_run_id()
    resolved_namespace = namespace or default_namespace(definition.test_type)
    manifests = render_manifests(
        definition,
        source=source,
        source_suffix=source_path.suffix,
        run_id=resolved_run_id,
        image=image,
        namespace=resolved_namespace,
        service_account=service_account,
    )
    return Submission(
        run_id=resolved_run_id,
        namespace=resolved_namespace,
        image=image,
        manifests=tuple(manifests),
    )


def submit(cluster: ClusterClient, submission: Submission) -> None:
    """Create the run's objects, owner first.

    The owner's uid is only known once the API server has assigned it, so the remaining
    manifests are bound to it here rather than at render time. Until that binding
    happens there is nothing for the garbage collector to cascade from, which is why the
    owner is created first and alone.
    """
    cluster.ensure_namespace(namespace_manifest(submission.namespace))

    owner, *rest = submission.manifests
    created_owner = cluster.create(owner)
    owner_uid = created_owner.get("metadata", {}).get("uid", "")
    if not owner_uid:
        raise ClusterError(
            "the API server returned the run's owner object without a uid, so nothing "
            "can be bound to it for garbage collection"
        )

    for manifest in rest:
        cluster.create(bind_owner(dict(manifest), owner_uid))


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
    """Stream a run's logs until it ends, and return the container's exit code.

    Locating the pod by label selector rather than by the name ``submit`` happened to
    create is what makes this usable from a process that never submitted anything.
    """
    pod = _await_pod(cluster, namespace, run_id, timeout=timeout, sleep=sleep, clock=clock)
    pod_name = pod["metadata"]["name"]

    for line in cluster.stream_logs(namespace, pod_name, CONTAINER_NAME):
        write(line)

    return _await_exit_code(cluster, namespace, pod_name, sleep=sleep, clock=clock)


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
                    f"no run {run_id!r} in namespace {namespace!r}. It may have finished "
                    "and been cleaned up, or it may be in another namespace."
                )
            seen_job = True
        if clock() >= deadline:
            state = "not scheduled" if pod is None else f"still {_phase(pod)}"
            raise ClusterError(
                f"run {run_id!r} is {state} after {timeout:.0f}s. The run itself is "
                f"unaffected; `test-orchestrator attach {run_id}` picks it up again."
            )
        sleep(1.0)


def _await_exit_code(
    cluster: ClusterClient,
    namespace: str,
    pod_name: str,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout: float = 60.0,
) -> int:
    """Read the container's exit code once the log stream has ended.

    The stream closing and the status reporting a terminated container are two different
    events, and they are not simultaneous.
    """
    deadline = clock() + timeout
    while True:
        pod = cluster.read_pod(namespace, pod_name)
        if pod is None:
            raise ClusterError(
                f"pod {pod_name!r} disappeared before it reported an exit code, so the "
                "run's outcome cannot be read from the cluster."
            )
        code = _terminated_exit_code(pod)
        if code is not None:
            return code
        if _phase(pod) == "Failed":
            reason = pod.get("status", {}).get("reason", "")
            if reason == "DeadlineExceeded":
                raise ClusterError(
                    f"run {pod_name!r} hit its activeDeadlineSeconds and was stopped "
                    "before it could report an outcome."
                )
        if clock() >= deadline:
            raise ClusterError(
                f"pod {pod_name!r} finished streaming but never reported an exit code."
            )
        sleep(1.0)


def _phase(pod: dict[str, Any]) -> str:
    phase: str = pod.get("status", {}).get("phase", "Pending")
    return phase


def _terminated_exit_code(pod: dict[str, Any]) -> int | None:
    for status in pod.get("status", {}).get("containerStatuses", []) or []:
        if status.get("name") != CONTAINER_NAME:
            continue
        terminated = status.get("state", {}).get("terminated")
        if terminated is not None and "exitCode" in terminated:
            code: int = terminated["exitCode"]
            return code
    return None
