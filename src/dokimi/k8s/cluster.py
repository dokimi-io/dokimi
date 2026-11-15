"""The narrow slice of the Kubernetes API a submission actually uses.

Everything the CLI does to a cluster goes through :class:`ClusterClient`, and all of
it needs only namespaced rights except listing nodes. The
protocol exists so the submission and reattach paths can be exercised without a cluster —
a fake that records calls proves ``--dry-run`` touches nothing far more convincingly than
a comment saying it does not.

``kubernetes`` is imported inside :func:`connect`, not at module scope. A dry run should
not need a kubeconfig, a cluster or even a working client library, and the cheapest way
to guarantee that is for the import never to happen.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from dokimi.k8s.port_forward import Channel
    from dokimi.k8s.resources import ResourceClient

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

    def resources(self) -> ResourceClient:
        """The per-kind client, over the same connection, for ownership and cleanup.

        Two protocols, deliberately: this one is the CLI's task-level view (submit, find,
        stream) and never imports the client library until :func:`connect`;
        :class:`~dokimi.k8s.resources.ResourceClient` is the generic per-kind view that
        ownership, exclusivity and label-scoped deletion are written against.
        """

    def list_nodes(self) -> list[dict[str, Any]]:
        """Every Node object. Read-only, and the only cluster-scoped call a run makes."""

    def open_port(self, namespace: str, pod: str, port: int) -> Channel:
        """A channel to *port* of *pod*: one port-forward, for one connection.

        Takes ``create`` on ``pods/portforward`` in *namespace*: the submitter's, never the
        Job's, and only ``dokimi prometheus`` asks for it.
        """

    def may_port_forward(self, namespace: str) -> bool | None:
        """Whether :meth:`open_port` is allowed in *namespace*; ``None`` if that's unknown."""


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
