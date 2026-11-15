"""The real adapter's error handling, against stand-ins for the client library's APIs.

Everything else tests the submission path against ``FakeCluster``, which cannot tell us
how ``KubernetesCluster`` translates the library's failures. That translation is where
two of the review findings on #27 lived: a transport error escaping as a traceback (exit
1, read by CI as ``fail``), and a namespace check demanding cluster-wide ``create`` from a
user who only needed the namespace to exist. A run no longer creates namespaces at all.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import MaxRetryError, ProtocolError

from dokimi.k8s._kubernetes import KubernetesCluster
from dokimi.k8s.cluster import ClusterError
from dokimi.k8s.resources import REQUEST_TIMEOUT_SECONDS

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

    def read_namespace(self, name: str, **_: Any) -> dict[str, Any]:
        self._call("read_namespace")
        return {}

    def create_namespace(self, body: dict[str, Any]) -> dict[str, Any]:
        self._call("create_namespace")
        return body

    def list_namespaced_pod(self, namespace: str, **_: Any) -> Any:
        self._call("list_namespaced_pod")
        return client.V1PodList(items=[])

    def list_node(self, **_: Any) -> Any:
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


# -- request timeouts -------------------------------------------------------------


class RecordingApi:
    """Answers every method with an empty result, and records the timeout it was given."""

    def __init__(self) -> None:
        self.timeouts: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def method(*args: Any, **kwargs: Any) -> Any:
            self.timeouts[name] = kwargs.get("_request_timeout")
            if name.startswith("list_"):
                return SimpleNamespace(items=[])
            if name == "read_namespaced_pod_log":
                return ChunkedStream(b"done\n")
            return {}

        return method


def test_every_call_is_bounded_by_a_request_timeout() -> None:
    # A call that hangs would hold a run's phase open, however well the phase is bounded.
    api = RecordingApi()
    cluster = KubernetesCluster(client.ApiClient())
    cluster._core = api
    cluster._batch = api

    cluster.create({"kind": "ConfigMap", "metadata": {"name": "a", "namespace": "ns"}})
    cluster.create({"kind": "Job", "metadata": {"name": "a", "namespace": "ns"}})
    cluster.find_job("ns", "a=b")
    cluster.find_pod("ns", "a=b")
    cluster.read_pod("ns", "pod")
    cluster.list_nodes()
    list(cluster.stream_logs("ns", "pod", "orchestrator"))

    streamed = api.timeouts.pop("read_namespaced_pod_log")
    assert set(api.timeouts.values()) == {REQUEST_TIMEOUT_SECONDS}
    assert len(api.timeouts) == 6
    # The log stream bounds its connect, not its read: a run can be quiet for its tests.
    assert streamed == (REQUEST_TIMEOUT_SECONDS, None)


# -- port-forward ---------------------------------------------------------------------------


#: The channel a fake forward hands back: only its identity matters here.
CHANNEL: Any = object()


class Forward:
    """What the client library's ``portforward`` returns: a channel per port."""

    def __init__(self) -> None:
        self.asked: list[int] = []

    def socket(self, port: int) -> Any:
        self.asked.append(port)
        return CHANNEL


def forwarding() -> KubernetesCluster:
    """An adapter whose core API has the method a port-forward is opened through."""
    core = CoreApi()
    core.connect_get_namespaced_pod_portforward = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    return adapter(core)


def test_a_port_forward_hands_back_the_channel_to_its_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kubernetes.stream  # noqa: PLC0415

    forward = Forward()
    calls: list[tuple[Any, ...]] = []

    def portforward(method: Any, name: str, namespace: str, **kwargs: Any) -> Forward:
        calls.append((name, namespace, kwargs))
        return forward

    monkeypatch.setattr(kubernetes.stream, "portforward", portforward)

    channel = forwarding().open_port("dokimi-iperf2", "prometheus-pod", 9090)

    assert channel is CHANNEL
    assert forward.asked == [9090]
    assert calls == [("prometheus-pod", "dokimi-iperf2", {"ports": "9090"})]


def test_a_port_forward_that_never_opens_is_bounded_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The library sets no timeout on its websocket; a hung handshake must not hang us.
    import kubernetes.stream  # noqa: PLC0415

    never = threading.Event()

    def hangs(*args: Any, **kwargs: Any) -> Forward:
        never.wait(5)
        return Forward()

    monkeypatch.setattr(kubernetes.stream, "portforward", hangs)

    with pytest.raises(ClusterError, match=r"no answer from the API server within 0\.1s"):
        forwarding().open_port("dokimi-iperf2", "prometheus-pod", 9090, timeout=0.1)
    never.set()


def test_a_failed_port_forward_gives_the_librarys_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    # What the library raises for a refused handshake on a real API server (kind, 1.37):
    # status 0 and an error of its own, nothing that says it was refused. Whether it may
    # forward at all is asked up front instead (may_port_forward).
    import kubernetes.stream  # noqa: PLC0415

    def refused(*args: Any, **kwargs: Any) -> Forward:
        raise ApiException(status=0, reason="'NoneType' object has no attribute 'decode'")

    monkeypatch.setattr(kubernetes.stream, "portforward", refused)

    with pytest.raises(ClusterError) as error:
        forwarding().open_port("dokimi-iperf2", "prometheus-pod", 9090)

    assert str(error.value) == (
        "forwarding to port 9090 of Pod prometheus-pod in dokimi-iperf2: "
        "'NoneType' object has no attribute 'decode'"
    )


class AuthorizationApi:
    """Answers a SelfSubjectAccessReview as it was told to, and records what was asked."""

    def __init__(self, allowed: bool | None = None, failure: BaseException | None = None) -> None:
        self.allowed = allowed
        self.failure = failure
        self.asked: list[Any] = []
        self.timeouts: list[Any] = []

    def create_self_subject_access_review(self, body: Any, **kwargs: Any) -> Any:
        self.asked.append(body.spec.resource_attributes)
        self.timeouts.append(kwargs.get("_request_timeout"))
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(status=SimpleNamespace(allowed=self.allowed))


def asking(authorization: AuthorizationApi) -> KubernetesCluster:
    cluster = KubernetesCluster(client.ApiClient())
    cluster._authorization = authorization
    return cluster


@pytest.mark.parametrize("allowed", [True, False])
def test_whether_a_port_forward_is_allowed_is_asked_as_kubectl_auth_can_i_does(
    allowed: bool,
) -> None:
    authorization = AuthorizationApi(allowed=allowed)

    assert asking(authorization).may_port_forward("dokimi-iperf2") is allowed
    (attributes,) = authorization.asked
    assert (
        attributes.namespace,
        attributes.verb,
        attributes.resource,
        attributes.subresource,
    ) == ("dokimi-iperf2", "create", "pods", "portforward")
    assert authorization.timeouts == [REQUEST_TIMEOUT_SECONDS]


@pytest.mark.parametrize(
    "failure",
    [
        _api_error(404),
        MaxRetryError(None, "/apis/authorization.k8s.io", "connection refused"),  # type: ignore[arg-type]
    ],
)
def test_a_question_the_api_server_cant_answer_leaves_it_to_the_forwards(
    failure: BaseException,
) -> None:
    assert asking(AuthorizationApi(failure=failure)).may_port_forward("dokimi-iperf2") is None
