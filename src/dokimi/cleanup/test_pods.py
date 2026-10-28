"""Removing a run's test pods, and waiting until they are really gone.

A deleted pod keeps running through its grace period, and so can its traffic. A run may
only stop holding the cluster once nothing of it can still be sending, so removing a pod
here means waiting until the API server no longer has it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from dataclasses import dataclass

from dokimi.k8s.identity import COMPONENT_LABEL, RUN_ID_LABEL
from dokimi.k8s.ownership import OwnedObject, delete_objects, find_run_owned
from dokimi.k8s.resources import POD, ClusterError, ResourceClient
from dokimi.run_identity import RunId
from dokimi.testtypes.interface import Role

__all__ = [
    "POD_REMOVAL_TIMEOUT_SECONDS",
    "TEST_POD_COMPONENTS",
    "PodRemoval",
    "remove_test_pods",
    "test_pod_selector",
]

#: A test's pods, by their component label: a preflight probe's too. A run's other pods,
#: such as its kept Prometheus or a DaemonSet's, are not test traffic, and a controller
#: would only recreate them.
TEST_POD_COMPONENTS = frozenset(role.value for role in Role)

#: How long to wait for removed pods to be gone: their grace period, plus the API
#: server's and the kubelet's reaction time.
POD_REMOVAL_TIMEOUT_SECONDS = 60.0

_POLL_SECONDS = 2.0


def test_pod_selector(run_id: RunId) -> str:
    """The label selector for a run's test pods, for a command a person can run."""
    components = ",".join(sorted(TEST_POD_COMPONENTS))
    return f"{RUN_ID_LABEL}={run_id},{COMPONENT_LABEL} in ({components})"


@dataclass(frozen=True, slots=True)
class PodRemoval:
    """What :func:`remove_test_pods` did."""

    removed: tuple[OwnedObject, ...] = ()
    kept: tuple[OwnedObject, ...] = ()
    #: Why a pod that had to go may still exist; ``None`` once every one of them is gone.
    problem: str | None = None

    @property
    def complete(self) -> bool:
        return self.problem is None


def remove_test_pods(
    resources: ResourceClient,
    run_id: RunId,
    namespace: str,
    *,
    keep: Collection[str] = frozenset(),
    timeout: float = POD_REMOVAL_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PodRemoval:
    """Delete the run's test pods, except those of the tests in *keep*, and wait.

    Never raises: a pod that could not be deleted, or is still there at *timeout*, is the
    returned :attr:`PodRemoval.problem`, for the caller to keep the cluster claimed.
    """

    def find() -> list[OwnedObject]:
        return find_run_owned(
            resources, run_id, namespace=namespace, kinds=(POD,), components=TEST_POD_COMPONENTS
        )

    try:
        pods = find()
        kept = tuple(pod for pod in pods if pod.test_id in keep)
        report = delete_objects(resources, run_id, [pod for pod in pods if pod not in kept])
        removed = tuple(report.deleted)
        if report.failed:
            failures = "; ".join(f"{pod.name}: {error}" for pod, error in report.failed)
            problem = f"could not delete run {run_id}'s test pods: {failures}"
            return PodRemoval(removed, kept, problem)
        deadline = clock() + timeout
        while [pod for pod in find() if pod.test_id not in keep]:
            if clock() >= deadline:
                return PodRemoval(
                    removed, kept, f"run {run_id}'s test pods were still there after {timeout:g}s"
                )
            sleep(_POLL_SECONDS)
    except ClusterError as error:
        return PodRemoval(problem=f"could not remove run {run_id}'s test pods: {error}")
    return PodRemoval(removed, kept)
