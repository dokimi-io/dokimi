"""The real :class:`~test_orchestrator.k8s.cluster.ClusterClient`, over the official client.

Kept apart from the protocol it implements so that importing the protocol — which the
dry-run path does — never imports the client library, and so the parts worth testing
without a cluster are not tangled with the parts that cannot be.

Objects cross this boundary as plain dictionaries in both directions. The client library's
models are generated classes with snake_case attributes and their own serialization
rules; letting them past this module would mean every consumer knowing which of the two
spellings it is holding.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from kubernetes import client, config, watch  # noqa: F401  (watch: see stream_logs)
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException

from test_orchestrator.k8s.cluster import ClusterError

__all__ = ["KubernetesCluster"]

_ALREADY_EXISTS = 409
_NOT_FOUND = 404


class KubernetesCluster:
    """Six operations against a real cluster."""

    def __init__(self, api: client.ApiClient) -> None:
        self._api = api
        self._core = client.CoreV1Api(api)
        self._batch = client.BatchV1Api(api)

    @classmethod
    def connect(
        cls, *, kubeconfig: str | None = None, context: str | None = None
    ) -> KubernetesCluster:
        if kubeconfig is None and context is None:
            try:
                config.load_incluster_config()
                return cls(client.ApiClient())
            except ConfigException:
                pass
        try:
            config.load_kube_config(config_file=kubeconfig, context=context)
        except (ConfigException, FileNotFoundError) as error:
            raise ClusterError(
                f"no usable cluster credentials: {error}. Set KUBECONFIG, pass "
                "--kubeconfig, or run inside the cluster."
            ) from error
        return cls(client.ApiClient())

    # -- writes ---------------------------------------------------------------

    def ensure_namespace(self, manifest: Mapping[str, Any]) -> None:
        try:
            self._core.create_namespace(body=dict(manifest))
        except ApiException as error:
            if error.status == _ALREADY_EXISTS:
                return
            raise ClusterError(_explain(error, f"creating namespace {_name(manifest)}")) from error

    def create(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(manifest)
        kind = body.get("kind")
        namespace = body["metadata"]["namespace"]
        try:
            if kind == "ConfigMap":
                created = self._core.create_namespaced_config_map(namespace, body)
            elif kind == "Job":
                created = self._batch.create_namespaced_job(namespace, body)
            else:  # pragma: no cover - a submission creates nothing else
                raise ClusterError(f"cannot create a {kind!r}: no client is wired up for it")
        except ApiException as error:
            raise ClusterError(_explain(error, f"creating {kind} {_name(body)}")) from error
        return self._to_dict(created)

    # -- reads ----------------------------------------------------------------

    def find_job(self, namespace: str, selector: str) -> dict[str, Any] | None:
        try:
            jobs = self._batch.list_namespaced_job(namespace, label_selector=selector)
        except ApiException as error:
            raise ClusterError(_explain(error, f"looking for a Job in {namespace}")) from error
        return self._newest(jobs.items)

    def find_pod(self, namespace: str, selector: str) -> dict[str, Any] | None:
        try:
            pods = self._core.list_namespaced_pod(namespace, label_selector=selector)
        except ApiException as error:
            raise ClusterError(_explain(error, f"looking for a Pod in {namespace}")) from error
        return self._newest(pods.items)

    def read_pod(self, namespace: str, name: str) -> dict[str, Any] | None:
        try:
            pod = self._core.read_namespaced_pod(name, namespace)
        except ApiException as error:
            if error.status == _NOT_FOUND:
                return None
            raise ClusterError(_explain(error, f"reading Pod {name}")) from error
        return self._to_dict(pod)

    def stream_logs(self, namespace: str, pod: str, container: str) -> Iterator[str]:
        """Follow a container's log until it closes.

        ``_preload_content=False`` is the difference between streaming and waiting: with
        it the client hands back the raw response and lines arrive as the run produces
        them, without it the call blocks until the container has exited.
        """
        try:
            response = self._core.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                container=container,
                follow=True,
                _preload_content=False,
            )
        except ApiException as error:
            raise ClusterError(_explain(error, f"streaming logs from {pod}")) from error
        try:
            for line in response.stream():
                yield line.decode("utf-8", errors="replace").rstrip("\n")
        finally:
            response.release_conn()

    # -- helpers --------------------------------------------------------------

    def _to_dict(self, obj: Any) -> dict[str, Any]:
        serialized: dict[str, Any] = self._api.sanitize_for_serialization(obj)
        return serialized

    def _newest(self, items: list[Any]) -> dict[str, Any] | None:
        if not items:
            return None
        newest = max(items, key=lambda item: item.metadata.creation_timestamp)
        return self._to_dict(newest)


def _name(manifest: Mapping[str, Any]) -> str:
    name: str = manifest.get("metadata", {}).get("name", "<unnamed>")
    return name


def _explain(error: ApiException, what: str) -> str:
    """Turn an ApiException into something a reader can act on."""
    detail = (error.reason or "").strip()
    if error.status == 403:
        return (
            f"{what}: refused ({detail}). The credentials in use may not have "
            "permission in this namespace."
        )
    return f"{what}: {error.status} {detail}"
