"""Finding a run's kept Prometheus after the run, and the time its history covers.

A run's managed Prometheus stays after the run, with every series it scraped while the
run was on, until ``dokimi cleanup --prometheus`` removes it. It is reachable only inside
the cluster, so ``dokimi prometheus <run-id>`` forwards a local port to it. This module
finds what that command forwards to: the run's Prometheus pod, by its labels in the run's
namespace, and when the run started and ended, so a query looks at the right window.

Only labels are trusted, as everywhere a run is found again: a name alone could be
anyone's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dokimi.k8s.ownership import (
    COMPONENT_LABEL,
    PROMETHEUS_COMPONENT,
    claim_of,
    find_run_owned,
    is_run_owned,
    owner_name,
    run_selector,
)
from dokimi.k8s.resources import CONFIG_MAP, DEPLOYMENT, POD, NotFoundError, ResourceClient
from dokimi.run_identity import RunId

__all__ = ["KeptPrometheus", "find_kept_prometheus"]


@dataclass(frozen=True, slots=True)
class KeptPrometheus:
    """A run's Prometheus, as found after the run."""

    run_id: RunId
    namespace: str
    #: A ready pod to forward to; ``None`` while there is none, and :attr:`problem` says why.
    pod: str | None
    problem: str | None
    #: When the run started: its id says so, to the second.
    started_at: datetime
    #: When the run finished, as its owner records it; ``None`` if it is still running,
    #: or its owner is gone, since a run that kept nothing loses it once fetched.
    finished_at: datetime | None
    #: Whether the run's owner is still there, to tell those two apart.
    owner_found: bool


def find_kept_prometheus(
    resources: ResourceClient, run_id: RunId, namespace: str
) -> KeptPrometheus | None:
    """The run's Prometheus in *namespace*, or ``None`` if it has none any more.

    ``None`` means the run's Prometheus was removed, or the run never deployed one, for
    instance because the cluster's own Prometheus was used.
    """
    deployments = find_run_owned(
        resources,
        run_id,
        namespace=namespace,
        kinds=(DEPLOYMENT,),
        components={PROMETHEUS_COMPONENT},
    )
    if not deployments:
        return None
    pod, problem = _ready_pod(resources, run_id, namespace)
    owner = _owner(resources, run_id, namespace)
    return KeptPrometheus(
        run_id=run_id,
        namespace=namespace,
        pod=pod,
        problem=problem,
        started_at=run_id.started_at,
        finished_at=None if owner is None else claim_of(owner).finished_at,
        owner_found=owner is not None,
    )


def _ready_pod(
    resources: ResourceClient, run_id: RunId, namespace: str
) -> tuple[str | None, str | None]:
    """A ready pod of the run's Prometheus, or ``None`` and why there is none."""
    selector = f"{run_selector(run_id)},{COMPONENT_LABEL}={PROMETHEUS_COMPONENT}"
    pods = [pod for pod in resources.list(POD, selector, namespace) if is_run_owned(pod, run_id)]
    for pod in sorted(pods, key=lambda pod: str(pod["metadata"]["name"])):
        if _is_ready(pod) and not pod["metadata"].get("deletionTimestamp"):
            return str(pod["metadata"]["name"]), None
    if not pods:
        return None, "its Deployment has no pod: it may be scaled to zero, or still starting"
    phases = ", ".join(
        f"{pod['metadata']['name']} {(pod.get('status') or {}).get('phase', 'Unknown')}"
        for pod in pods
    )
    return None, f"none of its pods is ready ({phases})"


def _is_ready(pod: Mapping[str, Any]) -> bool:
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def _owner(resources: ResourceClient, run_id: RunId, namespace: str) -> dict[str, Any] | None:
    """The run's owner, if it is still there and still the run's."""
    try:
        owner = resources.get(CONFIG_MAP, namespace, owner_name(run_id))
    except NotFoundError:
        return None
    return owner if is_run_owned(owner, run_id) else None
