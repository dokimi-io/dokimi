"""Cluster discovery: the nodes a run can choose from, read once.

Selection is resolved **once, at the start of the run**, and the matrix does not
change if the cluster does. So discovery is a single read that produces an immutable
:class:`NodeInventory`, and everything downstream plans against that snapshot rather than
against the live cluster.

What is kept about each node is exactly what decides whether a test pod can land on it,
and nothing more:

* **cordoned** — ``spec.unschedulable``, set by ``kubectl cordon``;
* **not Ready** — the kubelet's ``Ready`` condition is ``False`` or ``Unknown``, or absent;
* **tainted** — a ``NoSchedule`` or ``NoExecute`` taint. Test pods carry no tolerations,
  so either one keeps them off. ``PreferNoSchedule`` is only a preference and does not.

Whether the node has *room* for the pod is a different question with a different
answer — it depends on requests, other workloads and timing — and it belongs to preflight
(M1-07), which asks the scheduler rather than guessing on its behalf.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "BLOCKING_TAINT_EFFECTS",
    "NodeFacts",
    "NodeInventory",
    "NodeSource",
    "Taint",
    "discover",
    "node_facts",
]

#: Taint effects that keep a pod without tolerations off a node.
BLOCKING_TAINT_EFFECTS = frozenset({"NoSchedule", "NoExecute"})

#: The taint Kubernetes itself adds to a cordoned node. It is the cordon restated, not a
#: second problem, so it is not reported separately from it.
_CORDON_TAINT = "node.kubernetes.io/unschedulable"


class NodeSource(Protocol):
    """Anything that can list the cluster's nodes as API-shaped dictionaries.

    Deliberately one method rather than the whole cluster client: planning needs to read
    nodes and nothing else, and depending on the narrow thing keeps it that way.
    """

    def list_nodes(self) -> list[dict[str, Any]]:
        """Every Node object, in the API's own camelCase shape."""
        ...


@dataclass(frozen=True, slots=True)
class Taint:
    key: str
    value: str
    effect: str

    @property
    def blocks_scheduling(self) -> bool:
        return self.effect in BLOCKING_TAINT_EFFECTS

    def __str__(self) -> str:
        value = f"={self.value}" if self.value else ""
        return f"{self.key}{value}:{self.effect}"


@dataclass(frozen=True, slots=True)
class NodeFacts:
    """What decides whether a test pod can be placed on one node."""

    name: str
    cordoned: bool
    ready: bool
    taints: tuple[Taint, ...] = ()

    @property
    def blocking_taints(self) -> tuple[Taint, ...]:
        return tuple(taint for taint in self.taints if taint.blocks_scheduling)

    @property
    def unschedulable_reasons(self) -> tuple[str, ...]:
        """Every reason a test pod cannot land here, most actionable first."""
        reasons: list[str] = []
        if self.cordoned:
            reasons.append("it is cordoned (`kubectl uncordon` reverses that)")
        if not self.ready:
            reasons.append("it is not Ready")
        reasons.extend(
            f"it has the taint {taint}, and test pods carry no tolerations"
            for taint in self.blocking_taints
            if not (self.cordoned and taint.key == _CORDON_TAINT)
        )
        return tuple(reasons)

    @property
    def schedulable(self) -> bool:
        return not self.unschedulable_reasons


@dataclass(frozen=True, slots=True)
class NodeInventory:
    """The cluster's nodes at the moment discovery ran, sorted by name.

    Sorted because the matrix must be a pure function of the selection
    and the mode. The API returns nodes in whatever order it likes; nothing downstream
    should be able to observe that order.
    """

    nodes: tuple[NodeFacts, ...]

    @classmethod
    def of(cls, nodes: Iterable[NodeFacts]) -> NodeInventory:
        return cls(tuple(sorted(nodes, key=lambda node: node.name)))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(node.name for node in self.nodes)

    def get(self, name: str) -> NodeFacts | None:
        for node in self.nodes:
            if node.name == name:
                return node
        return None


def discover(source: NodeSource) -> NodeInventory:
    """Read the cluster's nodes, once."""
    return NodeInventory.of(node_facts(manifest) for manifest in source.list_nodes())


def node_facts(manifest: Mapping[str, Any]) -> NodeFacts:
    """Reduce a Node object to the facts that decide schedulability."""
    spec = manifest.get("spec") or {}
    status = manifest.get("status") or {}
    return NodeFacts(
        name=manifest["metadata"]["name"],
        cordoned=bool(spec.get("unschedulable", False)),
        ready=_is_ready(status.get("conditions") or []),
        taints=tuple(
            Taint(
                key=taint.get("key", ""),
                value=taint.get("value") or "",
                effect=taint.get("effect", ""),
            )
            for taint in spec.get("taints") or []
        ),
    )


def _is_ready(conditions: Iterable[Mapping[str, Any]]) -> bool:
    """``True`` only for an explicit ``Ready=True``.

    ``Unknown`` means the control plane has lost contact with the kubelet, and a node
    that reports no Ready condition at all is not one anything should be scheduled on.
    Both count as not Ready rather than as "probably fine".
    """
    for condition in conditions:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False
