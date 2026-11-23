"""The real :class:`~dokimi.k8s.cluster.ClusterClient`, over the official client.

Kept apart from the protocol it implements so that importing the protocol — which the
dry-run path does — never imports the client library, and so the parts worth testing
without a cluster are not tangled with the parts that cannot be.

Objects cross this boundary as plain dictionaries in both directions. The client library's
models are generated classes with snake_case attributes and their own serialization
rules; letting them past this module would mean every consumer knowing which of the two
spellings it is holding.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.config.config_exception import ConfigException
from urllib3.exceptions import HTTPError as TransportError

from dokimi.k8s.cluster import ClusterError
from dokimi.k8s.port_forward import Channel
from dokimi.k8s.resources import (
    REQUEST_TIMEOUT_SECONDS,
    KubernetesResources,
    bounded_api_client,
)

__all__ = ["KubernetesCluster"]

_NOT_FOUND = 404


class KubernetesCluster:
    """The CLI's operations against a real cluster.

    Every method translates both ways the client library can fail into
    :class:`ClusterError`: an ``ApiException`` when the API server answered and refused,
    and a urllib3 error when it could not be reached or dropped the connection. The
    second kind is easy to miss — it is not an ``ApiException`` — and an untranslated one
    escapes as a traceback, which Python exits with 1: ``fail`` in the exit-code table, for
    a run that nobody measured.
    """

    def __init__(self, api: client.ApiClient) -> None:
        self._api = api
        self._core = client.CoreV1Api(api)
        self._batch = client.BatchV1Api(api)
        self._authorization = client.AuthorizationV1Api(api)

    @classmethod
    def connect(
        cls, *, kubeconfig: str | None = None, context: str | None = None
    ) -> KubernetesCluster:
        if kubeconfig is None and context is None:
            try:
                config.load_incluster_config()
                return cls(bounded_api_client())
            except ConfigException:
                pass
        try:
            config.load_kube_config(config_file=kubeconfig, context=context)
        except (ConfigException, FileNotFoundError) as error:
            raise ClusterError(
                f"no usable cluster credentials: {error}. Set KUBECONFIG, pass "
                "--kubeconfig, or run inside the cluster."
            ) from error
        return cls(bounded_api_client())

    # -- writes ---------------------------------------------------------------

    def create(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(manifest)
        kind = body.get("kind")
        namespace = body["metadata"]["namespace"]
        with _transport(f"creating {kind} {_name(body)}"):
            try:
                if kind == "ConfigMap":
                    created = self._core.create_namespaced_config_map(
                        namespace, body, _request_timeout=REQUEST_TIMEOUT_SECONDS
                    )
                elif kind == "Job":
                    created = self._batch.create_namespaced_job(
                        namespace, body, _request_timeout=REQUEST_TIMEOUT_SECONDS
                    )
                else:  # pragma: no cover - a submission creates nothing else
                    raise ClusterError(f"cannot create a {kind!r}: no client is wired up for it")
            except ApiException as error:
                if error.status == _NOT_FOUND:
                    raise ClusterError(
                        f"namespace {namespace!r} does not exist. A run never creates a "
                        "namespace (ADR-05-13): an administrator creates it once, with "
                        "deploy/rbac.yaml for the default one, and after that submitting "
                        "needs only namespaced permissions."
                    ) from error
                raise ClusterError(_explain(error, f"creating {kind} {_name(body)}")) from error
        return self._to_dict(created)

    # -- reads ----------------------------------------------------------------

    def find_job(self, namespace: str, selector: str) -> dict[str, Any] | None:
        try:
            with _transport(f"looking for a Job in {namespace}"):
                jobs = self._batch.list_namespaced_job(
                    namespace, label_selector=selector, _request_timeout=REQUEST_TIMEOUT_SECONDS
                )
        except ApiException as error:
            raise ClusterError(_explain(error, f"looking for a Job in {namespace}")) from error
        return self._newest(jobs.items)

    def find_pod(self, namespace: str, selector: str) -> dict[str, Any] | None:
        try:
            with _transport(f"looking for a Pod in {namespace}"):
                pods = self._core.list_namespaced_pod(
                    namespace, label_selector=selector, _request_timeout=REQUEST_TIMEOUT_SECONDS
                )
        except ApiException as error:
            raise ClusterError(_explain(error, f"looking for a Pod in {namespace}")) from error
        return self._newest(pods.items)

    def read_pod(self, namespace: str, name: str) -> dict[str, Any] | None:
        try:
            with _transport(f"reading Pod {name}"):
                pod = self._core.read_namespaced_pod(
                    name, namespace, _request_timeout=REQUEST_TIMEOUT_SECONDS
                )
        except ApiException as error:
            if error.status == _NOT_FOUND:
                return None
            raise ClusterError(_explain(error, f"reading Pod {name}")) from error
        return self._to_dict(pod)

    def stream_logs(self, namespace: str, pod: str, container: str) -> Iterator[str]:
        """Follow a container's log until it closes, one line at a time.

        ``_preload_content=False`` is the difference between streaming and waiting: with
        it the client hands back the raw response and lines arrive as the run produces
        them, without it the call blocks until the container has exited.

        The response yields HTTP chunks, not lines: one chunk can hold many lines, and a
        line, or a character inside it, can be split across two. So the bytes are
        buffered and split on newlines before anything is decoded.
        """
        try:
            with _transport(f"streaming logs from {pod}"):
                response = self._core.read_namespaced_pod_log(
                    name=pod,
                    namespace=namespace,
                    container=container,
                    follow=True,
                    _preload_content=False,
                    # Connecting is bounded; reading is not, since a run can be quiet for
                    # as long as its tests take.
                    _request_timeout=(REQUEST_TIMEOUT_SECONDS, None),
                )
        except ApiException as error:
            raise ClusterError(_explain(error, f"streaming logs from {pod}")) from error
        try:
            # The connection can drop mid-stream, long after the call above succeeded.
            with _transport(f"streaming logs from {pod}"):
                pending = b""
                for chunk in response.stream():
                    *lines, pending = (pending + chunk).split(b"\n")
                    for line in lines:
                        yield line.decode("utf-8", errors="replace")
                if pending:
                    yield pending.decode("utf-8", errors="replace")
        finally:
            response.release_conn()

    def resources(self) -> KubernetesResources:
        """The per-kind client over the same connection and credentials."""
        return KubernetesResources(self._api)

    def open_port(
        self, namespace: str, pod: str, port: int, *, timeout: float = REQUEST_TIMEOUT_SECONDS
    ) -> Channel:
        """A channel to *port* of *pod*, over a websocket port-forward of its own.

        The client library sets no timeout on the websocket it opens, so the opening runs
        on a thread of its own and is given *timeout*, like every other call here. One that
        answers later is left to close with its thread.
        """
        from kubernetes.stream import portforward

        what = f"forwarding to port {port} of Pod {pod} in {namespace}"
        opened: list[Any] = []
        failed: list[BaseException] = []

        def open_forward() -> None:
            try:
                opened.append(
                    portforward(
                        self._core.connect_get_namespaced_pod_portforward,
                        pod,
                        namespace,
                        ports=str(port),
                    )
                )
            except Exception as error:  # handed to the caller's thread below
                failed.append(error)

        worker = threading.Thread(target=open_forward, name="dokimi-port-forward", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise ClusterError(f"{what}: no answer from the API server within {timeout:g}s")
        if failed:
            error = failed[0]
            # The library wraps every failure as an ApiException with status 0 and an error
            # of its own as the reason, a refused handshake included, so nothing here says
            # whether it was refused. That is asked up front, by may_port_forward.
            reason = str(getattr(error, "reason", None) or error)
            raise ClusterError(f"{what}: {reason}") from error
        channel: Channel = opened[0].socket(port)
        return channel

    def may_port_forward(self, namespace: str) -> bool | None:
        """Whether the credentials in use may open a port-forward in *namespace*.

        Asked of the API server, as ``kubectl auth can-i create pods/portforward`` does,
        which any authenticated user may ask. ``None`` when it couldn't be asked: then the
        forwards themselves say what they meet.
        """
        review = client.V1SelfSubjectAccessReview(
            spec=client.V1SelfSubjectAccessReviewSpec(
                resource_attributes=client.V1ResourceAttributes(
                    namespace=namespace,
                    verb="create",
                    resource="pods",
                    subresource="portforward",
                )
            )
        )
        try:
            answer = self._authorization.create_self_subject_access_review(
                review, _request_timeout=REQUEST_TIMEOUT_SECONDS
            )
        except ApiException, TransportError:
            return None
        return bool(answer.status.allowed)

    def list_nodes(self) -> list[dict[str, Any]]:
        """Every node in the cluster.

        Nodes are cluster-scoped, so this needs a ClusterRole where every other call
        here needs only a namespaced Role. A refusal is therefore the most likely RBAC
        failure a run meets, and it gets a message that says what to grant.
        """
        try:
            with _transport("listing nodes"):
                nodes = self._core.list_node(_request_timeout=REQUEST_TIMEOUT_SECONDS)
        except ApiException as error:
            if error.status == 403:
                raise ClusterError(
                    "listing nodes was refused. Planning resolves the definition's nodes "
                    "against the cluster, which needs `get` and `list` on nodes — a "
                    "cluster-wide permission. deploy/rbac.yaml grants exactly that; apply "
                    "it, then submit with --service-account dokimi."
                ) from error
            raise ClusterError(_explain(error, "listing nodes")) from error
        return [self._to_dict(node) for node in nodes.items]

    # -- helpers --------------------------------------------------------------

    def _to_dict(self, obj: Any) -> dict[str, Any]:
        serialized: dict[str, Any] = self._api.sanitize_for_serialization(obj)
        return serialized

    def _newest(self, items: list[Any]) -> dict[str, Any] | None:
        if not items:
            return None
        newest = max(items, key=lambda item: item.metadata.creation_timestamp)
        return self._to_dict(newest)


@contextmanager
def _transport(what: str) -> Iterator[None]:
    """Translate "could not reach the API server" into :class:`ClusterError`."""
    try:
        yield
    except TransportError as error:
        raise ClusterError(
            f"{what}: lost contact with the Kubernetes API server ({type(error).__name__}: {error})"
        ) from error


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
