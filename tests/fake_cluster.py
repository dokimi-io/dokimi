"""A cluster that only exists in memory, for the tests that must not need a real one.

The submission path's interesting properties — what gets created and in what order, that
a dry run creates nothing, that following a run finds its pod by label rather than by
name — are all statements about the calls made to a cluster. A fake that records those
calls tests them directly; a real cluster would test them incidentally, and only where
one is available.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from dokimi.k8s.cluster import ClusterError
from dokimi.k8s.resources import CONFIG_MAP, JOB
from dokimi.k8s.submission import CONTAINER_NAME
from tests.fake_resources import FakeResources

__all__ = ["ExplodingCluster", "FakeCluster", "node"]


def node(
    name: str,
    *,
    ready: str | None = "True",
    cordoned: bool = False,
    taints: Sequence[tuple[str, str, str]] = (),
) -> dict[str, Any]:
    """A Node object in the API's shape, with only the fields discovery reads.

    ``ready`` is the Ready condition's status string — ``"True"``, ``"False"``,
    ``"Unknown"`` — or ``None`` for a node that reports no Ready condition at all.
    ``taints`` are ``(key, value, effect)``.
    """
    spec: dict[str, Any] = {}
    if cordoned:
        spec["unschedulable"] = True
    if taints:
        spec["taints"] = [
            {"key": key, "value": value, "effect": effect} for key, value, effect in taints
        ]
    conditions = [] if ready is None else [{"type": "Ready", "status": ready}]
    return {"metadata": {"name": name}, "spec": spec, "status": {"conditions": conditions}}


#: The nodes the shipped example names, both able to take a test pod.
EXAMPLE_NODES = (node("worker-01"), node("worker-02"))


class FakeCluster:
    """Records every write and answers reads from a script."""

    def __init__(
        self,
        *,
        log_lines: Sequence[str] = (),
        exit_code: int = 0,
        phases: Sequence[str] = ("Running",),
        pod_name: str = "run-pod-1",
        has_pod: bool = True,
        has_job: bool = True,
        terminated_after: int = 0,
        owner_uid: str = "owner-uid-1",
        termination_reason: str = "",
        has_pod_after_stream: bool = True,
        job_conditions: Sequence[dict[str, str]] = (),
        fail_on_kind: str | None = None,
        fail_deleting: bool = False,
        nodes: Sequence[dict[str, Any]] = EXAMPLE_NODES,
        resources: FakeResources | None = None,
        forwarded: Callable[[socket.socket], None] | None = None,
    ) -> None:
        self.log_lines = list(log_lines)
        self.exit_code = exit_code
        self.phases = list(phases)
        self.pod_name = pod_name
        self.has_pod = has_pod
        self.has_job = has_job
        self.terminated_after = terminated_after
        self.owner_uid = owner_uid
        self.termination_reason = termination_reason
        self.has_pod_after_stream = has_pod_after_stream
        self.job_conditions = list(job_conditions)
        self.fail_on_kind = fail_on_kind
        #: The same cluster, as the per-kind client sees it. Everything :meth:`create`
        #: creates is also stored here, so ownership and exclusivity code sees it too.
        self.store = resources if resources is not None else FakeResources()
        self.store.fail_deleting = fail_deleting
        self.nodes = list(nodes)
        self.node_reads = 0
        #: What a port-forward reaches: it gets the far end of each channel, on a thread of
        #: its own, and answers on it as the pod would. ``None``: every forward is refused.
        self.forwarded = forwarded
        #: ``(namespace, pod, port)`` of every port-forward opened.
        self.forwards: list[tuple[str, str, int]] = []

        self.created: list[dict[str, Any]] = []
        self.selectors: list[str] = []
        self.streamed: list[tuple[str, str, str]] = []
        self._pod_reads = 0
        self._pod_lookups = 0

    # -- ClusterClient --------------------------------------------------------

    def create(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        if manifest["kind"] == self.fail_on_kind:
            raise ClusterError(f"creating {manifest['kind']}: 422 Unprocessable Entity")
        namespace = manifest["metadata"]["namespace"]
        self.store.namespaces.add(namespace)
        kind = CONFIG_MAP if manifest["kind"] == "ConfigMap" else JOB
        self.store.put(kind, namespace, dict(manifest))
        stored = dict(manifest)
        metadata = dict(stored["metadata"])
        metadata["uid"] = self.owner_uid if not self.created else f"uid-{len(self.created)}"
        stored["metadata"] = metadata
        self.created.append(stored)
        return stored

    def find_job(self, namespace: str, selector: str) -> dict[str, Any] | None:
        self.selectors.append(selector)
        if not self.has_job:
            return None
        return {
            "metadata": {"name": "run-job", "namespace": namespace, "labels": {}},
            "status": {"conditions": list(self.job_conditions)},
        }

    def find_pod(self, namespace: str, selector: str) -> dict[str, Any] | None:
        self.selectors.append(selector)
        if not self.has_pod:
            return None
        phase = self.phases[min(self._pod_lookups, len(self.phases) - 1)]
        self._pod_lookups += 1
        return {
            "metadata": {"name": self.pod_name, "namespace": namespace},
            "status": {"phase": phase},
        }

    def read_pod(self, namespace: str, name: str) -> dict[str, Any] | None:
        self._pod_reads += 1
        if not self.has_pod_after_stream:
            return None
        state: dict[str, Any] = (
            {"terminated": {"exitCode": self.exit_code, "reason": self.termination_reason}}
            if self._pod_reads > self.terminated_after
            else {"running": {}}
        )
        return {
            "metadata": {"name": name, "namespace": namespace},
            "status": {
                "phase": "Succeeded",
                "containerStatuses": [{"name": CONTAINER_NAME, "state": state}],
            },
        }

    def stream_logs(self, namespace: str, pod: str, container: str) -> Iterator[str]:
        self.streamed.append((namespace, pod, container))
        yield from self.log_lines

    def resources(self) -> FakeResources:
        return self.store

    def list_nodes(self) -> list[dict[str, Any]]:
        self.node_reads += 1
        return list(self.nodes)

    def open_port(self, namespace: str, pod: str, port: int) -> socket.socket:
        self.forwards.append((namespace, pod, port))
        if self.forwarded is None:
            raise ClusterError(f"forwarding to port {port} of Pod {pod}: refused")
        near, far = socket.socketpair()
        threading.Thread(target=self.forwarded, args=(far,), daemon=True).start()
        return near

    # -- assertions helpers ---------------------------------------------------

    @property
    def created_kinds(self) -> list[str]:
        return [manifest["kind"] for manifest in self.created]

    @property
    def deleted(self) -> list[tuple[str, str, str]]:
        """``(kind, namespace, name)`` of every object deleted through :meth:`resources`."""
        return self.store.deleted


class ExplodingCluster:
    """Fails on contact. Anything that must not touch a cluster gets one of these."""

    def _explode(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("this code path must not contact a cluster")

    create = _explode
    resources = _explode
    find_job = _explode
    find_pod = _explode
    read_pod = _explode
    stream_logs = _explode
    delete_config_maps = _explode
    list_nodes = _explode
    open_port = _explode
