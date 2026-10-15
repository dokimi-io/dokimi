"""Deploying a pair, the per-test readiness gate, and waiting for completion.

The test-type-independent half of phase 6. A plugin says *what* to run
(:class:`~dokimi.testtypes.interface.WorkloadSpec`) and *how* to start and observe it;
this module is where every test type is held to the same order:

1. :func:`deploy` creates the pods, bounded by a deadline and run-owned ([ADR-05-02])
   by construction, since it is the only path that creates them;
2. :func:`await_ready` is the **per-test readiness gate** ([ADR-05-07]): both pods
   ``Ready`` via the Kubernetes API, within an explicit timeout, or a
   :class:`ReadinessGateError` for that test;
3. :func:`start_when_ready` calls ``start()`` only with what the gate returned, so
   there is no path on which ``start()`` sees a pod that did not pass it;
4. :func:`wait_for_completion` polls ``is_complete()``.

A pod that never becomes ``Ready`` is an error for that test — not absent data, and not
a network failure. A pod whose image failed to pull :data:`IMAGE_PULL_ATTEMPTS` times, or
whose image reference no retry can fix, fails the gate then, rather than at its timeout.
Which error is a machine-readable :class:`ErrorCategory`, not only
text: a pod whose image cannot be pulled is ``image_pull``, ADR-05-05's own category,
and anything else is ``scheduling``. "Unschedulable: node(s) had untolerated taint" and
"ImagePullBackOff" point at different fixes, and ADR-05-07's preflight, which reuses
this gate for the first pair, must keep them apart. ADR-05-07's per-test clause calls
every pod that never becomes ``Ready`` a ``scheduling`` error; OQ-139 records the
conflict, and settling it either way is a change to :data:`_IMAGE_PULL_REASONS`.
"""

from __future__ import annotations

import enum
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
    "IMAGE_PULL_ATTEMPTS",
    "Completes",
    "CompletionTimeoutError",
    "ErrorCategory",
    "NotReadyError",
    "ReadinessGateError",
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

#: The kubelet's container-waiting reasons for an image it could not get. Each one is
#: about the image reference, the registry or its credentials, never about placement.
_IMAGE_PULL_REASONS = frozenset(
    {
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "ErrImageNeverPull",
        "ImageInspectError",
        "RegistryUnavailable",
        "SignatureValidationFailed",
    }
)


#: How many failed pulls of a pod's image the gate sits through before it gives up. The
#: kubelet retries a failed pull with a back-off that doubles from 10 s; measured on kind,
#: the third failure came 50 s after the first, and each stayed visible for about 12 s.
#: An image that failed three times is very unlikely to arrive in what is left of the
#: timeout.
IMAGE_PULL_ATTEMPTS = 3

#: The waiting reasons that mean a pull was just tried and failed. ``ImagePullBackOff`` is
#: the wait between two tries, so a pod whose pulls keep failing alternates between it
#: and one of these, and each return to one of these is one more failed attempt.
_PULL_FAILED_REASONS = frozenset({"ErrImagePull", "RegistryUnavailable"})

#: The reasons no retry can fix: a malformed reference, or a pull the pod forbids.
_UNPULLABLE_REASONS = frozenset({"InvalidImageName", "ErrImageNeverPull"})


class ErrorCategory(enum.Enum):
    """The two of ADR-05-05's error categories the readiness gate can produce.

    The whole taxonomy, and how it maps onto exit codes, is M1-16's; these are its values,
    spelled as ADR-05-05 spells them.
    """

    SCHEDULING = "scheduling"
    IMAGE_PULL = "image_pull"


class Starts(Protocol):
    """The part of :class:`~dokimi.testtypes.interface.TestTypePlugin` the gate calls."""

    def start(self, deployed: DeployedWorkload) -> None: ...


class Completes(Protocol):
    """The part of :class:`~dokimi.testtypes.interface.TestTypePlugin` completion polls."""

    def is_complete(self, deployed: DeployedWorkload) -> bool: ...


class ReadinessGateError(RuntimeError):
    """A pod of this test never became ``Ready``, and why, per pod and as a category."""

    def __init__(
        self,
        test_id: str,
        reasons: Mapping[Role, str],
        categories: Mapping[Role, ErrorCategory],
    ) -> None:
        self.test_id = test_id
        self.reasons = dict(reasons)
        self.categories = dict(categories)
        detail = "; ".join(f"{role.value}: {reason}" for role, reason in self.reasons.items())
        super().__init__(f"{test_id} {self._summary()}: {detail}")

    @property
    def category(self) -> ErrorCategory:
        """The test's category: ``image_pull`` if any pod could not get its image.

        A pod that cannot be placed has not tried to pull yet, so a pull failure next to
        it is the one finding that is certain, and both roles usually share the image.
        """
        if ErrorCategory.IMAGE_PULL in self.categories.values():
            return ErrorCategory.IMAGE_PULL
        return ErrorCategory.SCHEDULING

    def _summary(self) -> str:
        if self.category is ErrorCategory.IMAGE_PULL:
            return "could not pull its image"
        return "could not be scheduled"


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
    """The per-test readiness gate: both pods ``Ready``, or :class:`ReadinessGateError`.

    Fails early, rather than at the timeout, for a pod that can no longer become
    ``Ready`` — one that has terminated or been deleted — and for one that cannot get its
    image: after :data:`IMAGE_PULL_ATTEMPTS` failed pulls, or at once for a reference no
    retry can fix. A failed pull is counted each time the pod's reason returns to a
    pull failure. One the polls miss is not counted, which only makes the gate wait
    longer, never shorter.
    """
    deadline = clock() + timeout
    failed_pulls: dict[Role, int] = {}
    last_reasons: dict[Role, str] = {}
    while True:
        pods: dict[Role, Manifest] = {}
        blocked: dict[Role, str] = {}
        waiting: dict[Role, str] = {}
        categories: dict[Role, ErrorCategory] = {}
        for role in (Role.SERVER, Role.CLIENT):
            name = spec.pod_name(role)
            try:
                pod = cluster.get(POD, spec.namespace, name)
            except NotFoundError:
                blocked[role] = f"pod {name} no longer exists"
                categories[role] = ErrorCategory.SCHEDULING
                continue
            pods[role] = pod
            if pod_is_ready(pod):
                continue
            reason, why = _why(pod)
            categories[role] = (
                ErrorCategory.IMAGE_PULL
                if reason in _IMAGE_PULL_REASONS
                else ErrorCategory.SCHEDULING
            )
            if reason in _PULL_FAILED_REASONS and last_reasons.get(role) != reason:
                failed_pulls[role] = failed_pulls.get(role, 0) + 1
            last_reasons[role] = reason
            phase = (pod.get("status") or {}).get("phase")
            if phase in _TERMINAL_PHASES:
                blocked[role] = f"pod {name} ended ({phase}) before it was Ready: {why}"
            elif reason in _UNPULLABLE_REASONS:
                blocked[role] = f"pod {name} has an image no retry can pull: {why}"
            elif failed_pulls.get(role, 0) >= IMAGE_PULL_ATTEMPTS:
                blocked[role] = (
                    f"pod {name} failed to pull its image {failed_pulls[role]} times: {why}"
                )
            else:
                waiting[role] = f"pod {name} not Ready after {timeout:g}s: {why}"

        if blocked:
            raise ReadinessGateError(spec.test_id, blocked, _only(categories, blocked))
        if not waiting:
            return DeployedWorkload(spec=spec, pods=pods, cluster=cluster)
        if clock() >= deadline:
            raise ReadinessGateError(spec.test_id, waiting, _only(categories, waiting))
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


def _only(
    categories: Mapping[Role, ErrorCategory], roles: Mapping[Role, str]
) -> dict[Role, ErrorCategory]:
    """*categories* for the pods the error is about, not the one that was merely waiting."""
    return {role: categories[role] for role in roles}


def _why(pod: Manifest) -> tuple[str, str]:
    """The most specific reason a pod's status gives for not being Ready.

    Returns the bare reason, which classifies the failure, and the text a person reads.
    """
    status: dict[str, Any] = pod.get("status") or {}
    for container in status.get("containerStatuses") or []:
        state = container.get("state") or {}
        for key in ("waiting", "terminated"):
            detail = state.get(key)
            if detail and detail.get("reason"):
                return detail["reason"], _described(detail["reason"], detail.get("message"))
    for condition in status.get("conditions") or []:
        if condition.get("status") != "True" and condition.get("reason"):
            reason = condition["reason"]
            return reason, _described(reason, condition.get("message"))
    return "", f"phase {status.get('phase') or 'unknown'}"


def _described(reason: str, message: str | None) -> str:
    return reason + (f" ({message})" if message else "")
