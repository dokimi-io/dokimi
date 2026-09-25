"""The narrow slice of the Kubernetes API that run ownership needs.

This is the generic, per-kind counterpart of :mod:`test_orchestrator.k8s.cluster`. That
module is the CLI's view of a cluster — submit, find, stream, list nodes; this one is what
creating, finding and deleting arbitrary run-owned objects needs. Both raise the same
:class:`~test_orchestrator.k8s.cluster.ClusterError`, so a failure here reaches the CLI's
exit-code mapping unchanged.

Ownership, exclusivity and scoped deletion are written against :class:`ResourceClient`
rather than against the official client directly. That keeps the safety rules testable
without a cluster — the tests drive them against an in-memory fake with the same
conflict semantics — and keeps the official client's untyped, per-kind method sprawl in
one adapter, :class:`KubernetesResources`.

Objects cross this boundary as plain manifest dictionaries in the API's own camelCase
shape, the same shape a YAML manifest has.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from test_orchestrator.k8s.cluster import ClusterError

__all__ = [
    "CONFIG_MAP",
    "DAEMON_SET",
    "DEPLOYMENT",
    "JOB",
    "LEASE",
    "POD",
    "SERVICE",
    "SERVICE_ACCOUNT",
    "STATEFUL_SET",
    "AlreadyExistsError",
    "ClusterError",
    "ConflictError",
    "Kind",
    "KubernetesResources",
    "Manifest",
    "NotFoundError",
    "ResourceClient",
]

Manifest = dict[str, Any]


@dataclass(frozen=True)
class Kind:
    """A namespaced resource kind, and where the official client keeps its methods."""

    api_version: str
    kind: str
    #: The snake_case infix in the official client's method names, e.g. ``config_map``.
    method_name: str
    #: Which of the adapter's API groups serves it.
    api: str

    def __str__(self) -> str:
        return self.kind


CONFIG_MAP = Kind("v1", "ConfigMap", "config_map", "core")
POD = Kind("v1", "Pod", "pod", "core")
SERVICE = Kind("v1", "Service", "service", "core")
SERVICE_ACCOUNT = Kind("v1", "ServiceAccount", "service_account", "core")
JOB = Kind("batch/v1", "Job", "job", "batch")
DEPLOYMENT = Kind("apps/v1", "Deployment", "deployment", "apps")
DAEMON_SET = Kind("apps/v1", "DaemonSet", "daemon_set", "apps")
STATEFUL_SET = Kind("apps/v1", "StatefulSet", "stateful_set", "apps")
LEASE = Kind("coordination.k8s.io/v1", "Lease", "lease", "coordination")


class AlreadyExistsError(ClusterError):
    """``create`` found an object of that kind and name already there (HTTP 409)."""


class ConflictError(ClusterError):
    """An optimistic-concurrency precondition failed (HTTP 409 on replace or delete)."""


class NotFoundError(ClusterError):
    """The object does not exist (HTTP 404)."""


class ResourceClient(Protocol):
    """What ownership, exclusivity and cleanup need from a cluster, and nothing more."""

    def create(self, kind: Kind, namespace: str, body: Manifest) -> Manifest:
        """Create *body*; raise :class:`AlreadyExistsError` if the name is taken."""
        ...

    def get(self, kind: Kind, namespace: str, name: str) -> Manifest:
        """Read one object; raise :class:`NotFoundError` if it is absent."""
        ...

    def list(self, kind: Kind, label_selector: str, namespace: str | None = None) -> list[Manifest]:
        """List objects matching *label_selector*, in *namespace* or, if ``None``, everywhere."""
        ...

    def replace(self, kind: Kind, namespace: str, body: Manifest) -> Manifest:
        """Replace an object; *body* carries ``metadata.resourceVersion``.

        Raises :class:`ConflictError` if the object changed since that version was read.
        """
        ...

    def delete(
        self,
        kind: Kind,
        namespace: str,
        name: str,
        *,
        uid: str,
        resource_version: str | None = None,
    ) -> None:
        """Delete one object, only if it is still the object with *uid*.

        The uid precondition is what stops a delete from hitting a same-named object
        created after the one that was selected. *resource_version*, if given, also
        requires it to be unmodified since it was read. Raises :class:`NotFoundError` if
        it is already gone, :class:`ConflictError` if a precondition failed.
        """
        ...


class KubernetesResources:
    """:class:`ResourceClient` over the official ``kubernetes`` client."""

    def __init__(self, api_client: Any | None = None) -> None:
        from kubernetes import client

        self._api_client = api_client if api_client is not None else client.ApiClient()
        self._apis: dict[str, Any] = {
            "core": client.CoreV1Api(self._api_client),
            "apps": client.AppsV1Api(self._api_client),
            "batch": client.BatchV1Api(self._api_client),
            "coordination": client.CoordinationV1Api(self._api_client),
        }

    @classmethod
    def from_environment(cls) -> KubernetesResources:
        """In-cluster credentials inside the Job, the local kubeconfig anywhere else."""
        from kubernetes import config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        return cls()

    def create(self, kind: Kind, namespace: str, body: Manifest) -> Manifest:
        method = self._method(kind, f"create_namespaced_{kind.method_name}")
        return self._call(method, namespace=namespace, body=body, on_conflict=AlreadyExistsError)

    def get(self, kind: Kind, namespace: str, name: str) -> Manifest:
        method = self._method(kind, f"read_namespaced_{kind.method_name}")
        return self._call(method, name=name, namespace=namespace)

    def list(self, kind: Kind, label_selector: str, namespace: str | None = None) -> list[Manifest]:
        if namespace is None:
            method = self._method(kind, f"list_{kind.method_name}_for_all_namespaces")
            result = self._call(method, label_selector=label_selector)
        else:
            method = self._method(kind, f"list_namespaced_{kind.method_name}")
            result = self._call(method, namespace=namespace, label_selector=label_selector)
        items: list[Manifest] = result.get("items") or []
        return items

    def replace(self, kind: Kind, namespace: str, body: Manifest) -> Manifest:
        method = self._method(kind, f"replace_namespaced_{kind.method_name}")
        return self._call(method, name=body["metadata"]["name"], namespace=namespace, body=body)

    def delete(
        self,
        kind: Kind,
        namespace: str,
        name: str,
        *,
        uid: str,
        resource_version: str | None = None,
    ) -> None:
        from kubernetes import client

        method = self._method(kind, f"delete_namespaced_{kind.method_name}")
        options = client.V1DeleteOptions(
            preconditions=client.V1Preconditions(uid=uid, resource_version=resource_version),
            propagation_policy="Background",
        )
        self._call(method, name=name, namespace=namespace, body=options)

    def _method(self, kind: Kind, name: str) -> Any:
        return getattr(self._apis[kind.api], name)

    def _call(
        self,
        method: Any,
        *,
        on_conflict: type[ClusterError] = ConflictError,
        **kwargs: Any,
    ) -> Manifest:
        from kubernetes.client.exceptions import ApiException

        try:
            result = method(**kwargs)
        except ApiException as error:
            if error.status == 404:
                raise NotFoundError(error.reason) from error
            if error.status == 409:
                raise on_conflict(error.reason) from error
            raise ClusterError(f"HTTP {error.status}: {error.reason}") from error
        serialized: Manifest = self._api_client.sanitize_for_serialization(result) or {}
        return serialized
