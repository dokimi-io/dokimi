"""The narrow slice of the Kubernetes API a submission actually uses.

Everything a run does to a cluster goes through :class:`ClusterClient`: eight
operations, every namespaced one addressing objects by label selector rather than by name
(ADR-05-02). The
protocol exists so the submission and reattach paths can be exercised without a cluster —
a fake that records calls proves ``--dry-run`` touches nothing far more convincingly than
a comment saying it does not.

``kubernetes`` is imported inside :func:`connect`, not at module scope. A dry run should
not need a kubeconfig, a cluster or even a working client library, and the cheapest way
to guarantee that is for the import never to happen.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ClusterClient",
    "ClusterError",
    "connect",
]


class ClusterError(RuntimeError):
    """The cluster refused a request, or could not be reached at all."""


@runtime_checkable
class ClusterClient(Protocol):
    """What the CLI needs from a cluster. Nothing here is specific to a test type."""

    def ensure_namespace(self, manifest: Mapping[str, Any]) -> None:
        """Create the namespace if it is not already there."""

    def create(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        """Create one object and return it as the API server stored it."""

    def find_job(self, namespace: str, selector: str) -> dict[str, Any] | None:
        """The single Job matching *selector*, or ``None``."""

    def find_pod(self, namespace: str, selector: str) -> dict[str, Any] | None:
        """The most recent Pod matching *selector*, or ``None``."""

    def read_pod(self, namespace: str, name: str) -> dict[str, Any] | None:
        """One Pod by name, or ``None`` if it has gone."""

    def stream_logs(self, namespace: str, pod: str, container: str) -> Iterator[str]:
        """Yield log lines from a running or finished container until it ends."""

    def delete_config_maps(self, namespace: str, selector: str) -> None:
        """Delete the ConfigMaps matching *selector*, cascading to their dependents.

        By selector, never by name, like every delete a run makes (ADR-05-02).
        """

    def list_nodes(self) -> list[dict[str, Any]]:
        """Every Node object. Read-only, and the only cluster-scoped call a run makes."""


def connect(kubeconfig: str | None = None, context: str | None = None) -> ClusterClient:
    """Load cluster credentials and return a client.

    In-cluster service-account credentials are preferred when present, so the same code
    path works for a CI runner inside the cluster and a laptop outside it.
    """
    try:
        from dokimi.k8s._kubernetes import KubernetesCluster
    except ImportError as error:  # pragma: no cover - the dependency is not optional
        raise ClusterError(f"the Kubernetes client library is unavailable: {error}") from error

    return KubernetesCluster.connect(kubeconfig=kubeconfig, context=context)
