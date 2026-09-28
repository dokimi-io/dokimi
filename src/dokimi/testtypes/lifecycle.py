"""Deploying a pair, the per-test readiness gate, and waiting for completion.

The test-type-independent half of phase 6. A plugin says *what* to run
(:class:`~dokimi.testtypes.interface.WorkloadSpec`) and *how* to start and observe it;
this module is where every test type is held to the same order:

1. :func:`deploy` creates the pods, bounded by a deadline and run-owned ([ADR-05-02])
   by construction, since it is the only path that creates them;
2. :func:`await_ready` is the **per-test readiness gate** ([ADR-05-07]): both pods
   ``Ready`` via the Kubernetes API, within an explicit timeout, or a
   :class:`SchedulingError` for that test;
3. :func:`start_when_ready` calls ``start()`` only with what the gate returned, so
   there is no path on which ``start()`` sees a pod that did not pass it;
4. :func:`wait_for_completion` polls ``is_complete()``.

A pod that never becomes ``Ready`` is a ``scheduling`` error for that test
([ADR-05-05]) — not absent data, and not a network failure. The error carries what the
pod's status said, because "Unschedulable: node(s) had untolerated taint" and
"ImagePullBackOff" point at different fixes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from dokimi.k8s.deadlines import DEFAULT_STARTUP_SECONDS, DeadlineBudget, bound_pod
from dokimi.k8s.resources import POD, Manifest, NotFoundError, ResourceClient
from dokimi.testtypes.interface import DeployedWorkload, Role, WorkloadSpec

if TYPE_CHECKING:
    from dokimi.k8s.ownership import RunOwnership

__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_READINESS_TIMEOUT_SECONDS",
    "Completes",
    "CompletionTimeoutError",
    "NotReadyError",
    "SchedulingError",
    "Starts",
    "WorkloadLostError",
    "await_ready",
    "deploy",
    "pod_is_ready",
    "start_when_ready",
    "wait_for_completion",
]

#: The gate's timeout. The workload deadline's startup term already budgets scheduling,
#: image pull and this gate together ([ADR-05-13]), so the gate may use all of it and
#: never more: a gate that outlived the term would be waiting on pods the cluster is
#: about to kill. Distinct from ADR-03-01's scrape-confirmation window, which is its own
#: term of the same budget.
DEFAULT_READINESS_TIMEOUT_SECONDS = DEFAULT_STARTUP_SECONDS
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

_TERMINAL_PHASES = ("Succeeded", "Failed")


class Starts(Protocol):
    """The part of :class:`~dokimi.testtypes.interface.TestTypePlugin` the gate calls."""

    def start(self, deployed: DeployedWorkload) -> None: ...


class Completes(Protocol):
    """The part of :class:`~dokimi.testtypes.interface.TestTypePlugin` completion polls."""

    def is_complete(self, deployed: DeployedWorkload) -> bool: ...


class SchedulingError(RuntimeError):
    """A pod of this test never became ``Ready``: ADR-05-05's ``scheduling`` category."""

    def __init__(self, test_id: str, reasons: Mapping[Role, str]) -> None:
        self.test_id = test_id
        self.reasons = dict(reasons)
        detail = "; ".join(f"{role.value}: {reason}" for role, reason in self.reasons.items())
        super().__init__(f"{test_id} could not be scheduled: {detail}")


class NotReadyError(RuntimeError):
    """``start()`` was called with a pod that is not ``Ready``.

    A bug in the caller rather than a finding about the cluster: the gate exists so that
    this cannot happen, and ``start()`` checks again because it is the call that sends
    traffic.
    """


class WorkloadLostError(RuntimeError):
    """A pod of a started test disappeared before the test finished."""


class CompletionTimeoutError(RuntimeError):
    """A started test did not complete within its timeout."""


def pod_is_ready(pod: Manifest) -> bool:
    """Whether *pod* is running and its ``Ready`` condition is ``True``."""
    status = pod.get("status") or {}
    if status.get("phase") != "Running":
        return False
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in status.get("conditions") or []
    )


def deploy(ownership: RunOwnership, spec: WorkloadSpec, budget: DeadlineBudget) -> None:
    """Create *spec*'s pods, server first, each bounded and run-owned."""
    for role in (Role.SERVER, Role.CLIENT):
        ownership.create(
            POD,
            bound_pod(spec.pods[role], budget),
            test_id=spec.test_id,
            component=role.value,
        )


def await_ready(
    cluster: ResourceClient,
    spec: WorkloadSpec,
    *,
    timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DeployedWorkload:
    """The per-test readiness gate: both pods ``Ready``, or :class:`SchedulingError`.

    Fails early, rather than at the timeout, for a pod that can no longer become
    ``Ready`` — one that has terminated or been deleted.
    """
    deadline = clock() + timeout
    while True:
        pods: dict[Role, Manifest] = {}
        blocked: dict[Role, str] = {}
        waiting: dict[Role, str] = {}
        for role in (Role.SERVER, Role.CLIENT):
            name = spec.pod_name(role)
            try:
                pod = cluster.get(POD, spec.namespace, name)
            except NotFoundError:
                blocked[role] = f"pod {name} no longer exists"
                continue
            pods[role] = pod
            if pod_is_ready(pod):
                continue
            phase = (pod.get("status") or {}).get("phase")
            if phase in _TERMINAL_PHASES:
                blocked[role] = f"pod {name} ended ({phase}) before it was Ready: {_why(pod)}"
            else:
                waiting[role] = f"pod {name} not Ready after {timeout:g}s: {_why(pod)}"

        if blocked:
            raise SchedulingError(spec.test_id, blocked)
        if not waiting:
            return DeployedWorkload(spec=spec, pods=pods, cluster=cluster)
        if clock() >= deadline:
            raise SchedulingError(spec.test_id, waiting)
        sleep(poll_interval)


def start_when_ready(
    plugin: Starts,
    cluster: ResourceClient,
    spec: WorkloadSpec,
    *,
    timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DeployedWorkload:
    """Pass the readiness gate, then call ``start()``. A gate failure never starts."""
    deployed = await_ready(
        cluster, spec, timeout=timeout, poll_interval=poll_interval, clock=clock, sleep=sleep
    )
    plugin.start(deployed)
    return deployed


def wait_for_completion(
    plugin: Completes,
    deployed: DeployedWorkload,
    *,
    timeout: float,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll ``is_complete()`` until it is true, or raise :class:`CompletionTimeoutError`."""
    deadline = clock() + timeout
    while not plugin.is_complete(deployed):
        if clock() >= deadline:
            raise CompletionTimeoutError(
                f"{deployed.spec.test_id} did not complete within {timeout:g}s"
            )
        sleep(poll_interval)


def _why(pod: Manifest) -> str:
    """The most specific reason a pod's status gives for not being Ready."""
    status: dict[str, Any] = pod.get("status") or {}
    for container in status.get("containerStatuses") or []:
        state = container.get("state") or {}
        for key in ("waiting", "terminated"):
            detail = state.get(key)
            if detail and detail.get("reason"):
                message = detail.get("message")
                return f"{detail['reason']}" + (f" ({message})" if message else "")
    for condition in status.get("conditions") or []:
        if condition.get("status") != "True" and condition.get("reason"):
            message = condition.get("message")
            return f"{condition['reason']}" + (f" ({message})" if message else "")
    return f"phase {status.get('phase') or 'unknown'}"
