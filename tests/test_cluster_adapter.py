"""The real adapter's error handling, against stand-ins for the client library's APIs.

Everything else tests the submission path against ``FakeCluster``, which cannot tell us
how ``KubernetesCluster`` translates the library's failures. That translation is where
two of the review findings on #27 lived: a transport error escaping as a traceback (exit
1, read by CI as ``fail``), and a namespace check demanding cluster-wide ``create`` from a
user who only needed the namespace to exist. A run no longer creates namespaces at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import MaxRetryError, ProtocolError

from dokimi.k8s._kubernetes import KubernetesCluster
from dokimi.k8s.cluster import ClusterError

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

    def list_node(self) -> Any:
        self._call("list_node")
        return client.V1NodeList(items=[])

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


# -- namespaces -----------------------------------------------------------------


def test_the_adapter_never_creates_a_namespace() -> None:
    # ADR-05-13: a run never creates one. Creating a namespace is cluster-wide, and the
    # point of per-test-type namespaces is that submitting needs only namespaced rights.
    assert not hasattr(KubernetesCluster, "ensure_namespace")


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


class ChunkedStream:
    """What urllib3 hands back: HTTP chunks, which neither start nor end with lines."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    def stream(self) -> Iterator[bytes]:
        yield from self.chunks

    def release_conn(self) -> None:
        pass


def test_a_log_stream_yields_lines_whatever_the_chunks() -> None:
    # Merged lines, a line split across chunks, a character split across chunks, and a
    # last line without its newline.
    accented = "café\n".encode()
    chunks = [b"phase 2\nphase 3\n", b"par", b"tial\n", accented[:4], accented[4:], b"end"]

    class ChunkingApi(CoreApi):
        def read_namespaced_pod_log(self, **_: Any) -> Any:
            return ChunkedStream(*chunks)

    core = ChunkingApi()

    assert list(adapter(core).stream_logs("ns", "pod", "orchestrator")) == [
        "phase 2",
        "phase 3",
        "partial",
        "café",
        "end",
    ]


# -- listing nodes (M1-06) ------------------------------------------------------


def test_a_refused_node_list_names_the_fix() -> None:
    # The first cluster-scoped call a run makes, and so its most likely RBAC failure.
    core = CoreApi(list_node=_api_error(403))

    with pytest.raises(ClusterError, match=r"deploy/rbac\.yaml.*--service-account"):
        adapter(core).list_nodes()


def test_an_unreachable_api_server_while_listing_nodes_is_a_cluster_error() -> None:
    core = CoreApi(list_node=MaxRetryError(None, "/api/v1/nodes", "connection refused"))  # type: ignore[arg-type]

    with pytest.raises(ClusterError, match="listing nodes: lost contact"):
        adapter(core).list_nodes()
