"""The real adapter's error handling, against stand-ins for the client library's APIs.

Everything else tests the submission path against ``FakeCluster``, which cannot tell us
how ``KubernetesCluster`` translates the library's failures. That translation is where
two of the review findings on #27 lived: a transport error escaping as a traceback (exit
1, read by CI as ``fail``), and ``ensure_namespace`` demanding cluster-wide ``create``
from a user who only needed the namespace to exist.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import MaxRetryError, ProtocolError

from test_orchestrator.k8s._kubernetes import KubernetesCluster
from test_orchestrator.k8s.cluster import ClusterError

NAMESPACE = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "ns"}}


class CoreApi:
    """Records calls; raises whatever it was told to for each method."""

    def __init__(self, **failures: BaseException) -> None:
        self.failures = failures
        self.calls: list[str] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.failures:
            raise self.failures[name]

    def read_namespace(self, name: str) -> dict[str, Any]:
        self._call("read_namespace")
        return {}

    def create_namespace(self, body: dict[str, Any]) -> dict[str, Any]:
        self._call("create_namespace")
        return body

    def list_namespaced_pod(self, namespace: str, **_: Any) -> Any:
        self._call("list_namespaced_pod")
        return client.V1PodList(items=[])

    def read_namespaced_pod_log(self, **_: Any) -> Any:
        self._call("read_namespaced_pod_log")
        return DroppingStream()


class DroppingStream:
    """A log stream whose connection drops after one line."""

    released = False

    def stream(self) -> Iterator[bytes]:
        yield b"phase 2\n"
        raise ProtocolError("Connection broken: IncompleteRead")

    def release_conn(self) -> None:
        DroppingStream.released = True


def adapter(core: CoreApi) -> KubernetesCluster:
    cluster = KubernetesCluster(client.ApiClient())
    cluster._core = core
    return cluster


def _api_error(status: int) -> ApiException:
    return ApiException(status=status, reason={403: "Forbidden", 404: "Not Found"}.get(status, "x"))


# -- ensure_namespace -----------------------------------------------------------


def test_an_existing_namespace_is_not_created_again() -> None:
    # Creating a namespace is cluster-wide. Asking for it on every submit would demand
    # that right from users who need only the namespaced ones.
    core = CoreApi()

    adapter(core).ensure_namespace(NAMESPACE)

    assert core.calls == ["read_namespace"]


def test_a_missing_namespace_is_created() -> None:
    core = CoreApi(read_namespace=_api_error(404))

    adapter(core).ensure_namespace(NAMESPACE)

    assert core.calls == ["read_namespace", "create_namespace"]


def test_a_namespace_scoped_identity_that_cannot_read_it_is_not_refused() -> None:
    # Such an identity may use a namespace it cannot read. The creates that follow are
    # what establish whether the namespace really exists.
    core = CoreApi(read_namespace=_api_error(403))

    adapter(core).ensure_namespace(NAMESPACE)

    assert core.calls == ["read_namespace"]


def test_losing_a_race_to_create_the_namespace_is_fine() -> None:
    core = CoreApi(read_namespace=_api_error(404), create_namespace=_api_error(409))

    adapter(core).ensure_namespace(NAMESPACE)


def test_any_other_refusal_is_a_cluster_error() -> None:
    core = CoreApi(read_namespace=_api_error(500))

    with pytest.raises(ClusterError, match="checking namespace ns"):
        adapter(core).ensure_namespace(NAMESPACE)


# -- transport errors -----------------------------------------------------------


def test_an_unreachable_api_server_is_a_cluster_error() -> None:
    # urllib3's errors are not ApiExceptions. Untranslated, one escaped as a traceback and
    # Python exited 1 — `fail`, for a run that was never measured.
    core = CoreApi(list_namespaced_pod=MaxRetryError(None, "/api/v1", "connection refused"))  # type: ignore[arg-type]

    with pytest.raises(ClusterError, match="lost contact with the Kubernetes API server"):
        adapter(core).find_pod("ns", "a=b")


def test_a_log_stream_that_drops_is_a_cluster_error_and_releases_its_connection() -> None:
    core = CoreApi()
    received: list[str] = []

    with pytest.raises(ClusterError, match="lost contact"):
        for line in adapter(core).stream_logs("ns", "pod", "orchestrator"):
            received.append(line)

    assert received == ["phase 2"]
    assert DroppingStream.released
