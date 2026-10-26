"""The adapter over the official client, checked without a cluster.

The adapter builds method names from each :class:`Kind` at call time, so a typo in a
``method_name`` would only surface against a live API server. These tests resolve every
name it can build against the real client classes, and check the HTTP-status mapping the
safety logic relies on.
"""

from __future__ import annotations

from typing import Any

import pytest
from kubernetes.client.exceptions import ApiException

from dokimi.k8s.ownership import RUN_OWNED_KINDS
from dokimi.k8s.resources import (
    CONFIG_MAP,
    LEASE,
    REQUEST_TIMEOUT_SECONDS,
    AlreadyExistsError,
    ClusterError,
    ConflictError,
    Kind,
    KubernetesResources,
    NotFoundError,
    bounded_api_client,
)

_VERBS = (
    "create_namespaced_{}",
    "read_namespaced_{}",
    "list_namespaced_{}",
    "list_{}_for_all_namespaces",
    "replace_namespaced_{}",
    "delete_namespaced_{}",
)


@pytest.fixture
def adapter() -> KubernetesResources:
    from kubernetes import client

    return KubernetesResources(client.ApiClient(client.Configuration()))


@pytest.mark.parametrize("kind", [*RUN_OWNED_KINDS, LEASE], ids=str)
def test_every_method_the_adapter_builds_exists(adapter: KubernetesResources, kind: Kind) -> None:
    for verb in _VERBS:
        assert callable(adapter._method(kind, verb.format(kind.method_name)))


def _raising(status: int) -> Any:
    def method(**kwargs: Any) -> None:
        raise ApiException(status=status, reason=f"status {status}")

    return method


@pytest.mark.parametrize(
    ("status", "on_create", "otherwise"),
    [
        (404, NotFoundError, NotFoundError),
        (409, AlreadyExistsError, ConflictError),
        (403, ClusterError, ClusterError),
    ],
)
def test_http_errors_map_to_cluster_errors(
    adapter: KubernetesResources,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    on_create: type[ClusterError],
    otherwise: type[ClusterError],
) -> None:
    monkeypatch.setattr(adapter, "_method", lambda kind, name: _raising(status))

    with pytest.raises(on_create):
        adapter.create(CONFIG_MAP, "ns", {"metadata": {"name": "x"}})
    with pytest.raises(otherwise):
        adapter.delete(CONFIG_MAP, "ns", "x", uid="u")


def test_results_come_back_as_camel_case_manifests(
    adapter: KubernetesResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kubernetes import client

    pod_list = client.V1PodList(
        items=[
            client.V1Pod(
                metadata=client.V1ObjectMeta(name="p", namespace="ns", uid="u", owner_references=[])
            )
        ]
    )
    monkeypatch.setattr(adapter, "_method", lambda kind, name: lambda **kwargs: pod_list)

    (item,) = adapter.list(CONFIG_MAP, "a=b")

    assert item["metadata"] == {"name": "p", "namespace": "ns", "uid": "u", "ownerReferences": []}


def test_every_call_the_adapter_makes_has_a_request_timeout(
    adapter: KubernetesResources, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[object] = []

    def method(**kwargs: Any) -> dict[str, Any]:
        timeouts.append(kwargs.get("_request_timeout"))
        return {}

    monkeypatch.setattr(adapter, "_method", lambda kind, name: method)

    adapter.create(CONFIG_MAP, "ns", {"metadata": {"name": "x"}})
    adapter.get(CONFIG_MAP, "ns", "x")
    adapter.list(CONFIG_MAP, "a=b", "ns")
    adapter.list(CONFIG_MAP, "a=b")
    adapter.replace(CONFIG_MAP, "ns", {"metadata": {"name": "x"}})
    adapter.delete(CONFIG_MAP, "ns", "x", uid="u")

    assert timeouts == [REQUEST_TIMEOUT_SECONDS] * 6


def test_a_call_that_hangs_is_not_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # urllib3 retries a timed-out read three times by default: four timeouts, not one.
    from kubernetes import client

    monkeypatch.setattr(client.Configuration, "_default", client.Configuration())

    retries = bounded_api_client().configuration.retries

    assert retries.read == 0
    assert retries.total == 3
