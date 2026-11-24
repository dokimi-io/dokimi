"""Phase 3: turn a definition and a node inventory into the run's plan.

Milestone-01 plans exactly one thing — the one directed pair between two explicitly named
nodes, executed sequentially — and the code here is shaped by the two ways that could go
wrong.

**Too little.** A plan that is only ``(source, target)`` would have to be reshaped the
moment a second pair exists. So a :class:`Plan` is already a sequence of *waves*, each a
tuple of pairs that may run together. Sequential execution is one pair per wave; bounded
concurrency is more than one. The shape does not change, only the scheduler
that fills it.

**Too much.** The other matrix modes, label and topology selectors and concurrent
waves are Milestone-02. Nothing here names them: there is no ``PlacementMode`` enum with
three members that raise ``NotImplementedError``, because a concept the code carries is a
concept someone will start depending on.
"""

from __future__ import annotations

import difflib
import enum
from dataclasses import dataclass
from typing import Any

from dokimi.model import ConfigError, TestDefinition
from dokimi.planning.nodes import NodeFacts, NodeInventory

__all__ = [
    "PLACEMENT_MODE",
    "Concurrency",
    "Plan",
    "PlannedPair",
    "plan_run",
    "resolve_node",
]

#: The only placement mode Milestone-01 has. A string, not an enum member:
#: see the module docstring.
PLACEMENT_MODE = "explicit"

#: How many node names an error message lists before summarising the rest.
_LISTED_NODES = 10


class Concurrency(enum.Enum):
    """The execution strategy the plan was built for.

    Milestone-01 fixes this to sequential. The default, bounded concurrency with
    node exclusivity, arrives with the matrices that make it matter.
    """

    SEQUENTIAL = "sequential"


@dataclass(frozen=True, slots=True)
class PlannedPair:
    """One directed test: traffic flows from ``source`` to ``target``."""

    test_id: str
    source: NodeFacts
    target: NodeFacts


@dataclass(frozen=True, slots=True)
class Plan:
    """What the run will execute, and in what order.

    ``waves`` run one after another; the pairs within a wave run together. A plan is
    never empty — a selection that yields zero tests is an error, not a run
    that passes having measured nothing.
    """

    waves: tuple[tuple[PlannedPair, ...], ...]
    concurrency: Concurrency = Concurrency.SEQUENTIAL
    placement_mode: str = PLACEMENT_MODE

    def __post_init__(self) -> None:
        if not self.pairs:
            raise ConfigError("the plan has no tests in it, so the run would measure nothing")

    @property
    def pairs(self) -> tuple[PlannedPair, ...]:
        return tuple(pair for wave in self.waves for pair in wave)

    @property
    def max_concurrency(self) -> int:
        return max(len(wave) for wave in self.waves)

    def run_metadata(self) -> dict[str, Any]:
        """The fields the plan contributes to the run's metadata.

        The resolved selection and the effective concurrency are both recorded,
        because a result is not interpretable without either. M1-14 (#49) writes these to
        the artifact, next to the image fields M1-02 contributes.
        """
        return {
            "placement_mode": self.placement_mode,
            "concurrency": self.concurrency.value,
            "max_concurrency": self.max_concurrency,
            "planned_tests": len(self.pairs),
            "pairs": [
                {
                    "test_id": pair.test_id,
                    "wave": wave_index,
                    "source_node": pair.source.name,
                    "target_node": pair.target.name,
                }
                for wave_index, wave in enumerate(self.waves)
                for pair in wave
            ],
        }


def plan_run(definition: TestDefinition, inventory: NodeInventory) -> Plan:
    """Resolve the definition's nodes against the inventory and plan the run.

    Every failure here is a ``config_error``: the request names nodes the
    cluster cannot give it, and that is known before anything is deployed.
    """
    source = resolve_node(inventory, definition.source_node, role="source")
    target = resolve_node(inventory, definition.target_node, role="target")
    pair = PlannedPair(test_id=_test_id(0), source=source, target=target)
    return Plan(waves=((pair,),))


def resolve_node(inventory: NodeInventory, name: str, *, role: str) -> NodeFacts:
    """Find one explicitly named node and confirm a test pod can land on it."""
    node = inventory.get(name)
    if node is None:
        raise ConfigError(_missing(inventory, name, role))
    if not node.schedulable:
        reasons = "; ".join(node.unschedulable_reasons)
        raise ConfigError(f"{role} node {name!r} cannot accept a test pod: {reasons}")
    return node


def _test_id(index: int) -> str:
    """A test's identifier within its run (its ``test_id`` label).

    Positional, which is only meaningful because the order is deterministic:
    the same selection and mode always yield the same numbering. Zero-padded so the ids
    sort as the plan does, and short enough to sit in a label value however long the
    node names are.
    """
    return f"pair-{index + 1:03d}"


def _missing(inventory: NodeInventory, name: str, role: str) -> str:
    message = f"{role} node {name!r} does not exist in the cluster"
    if not inventory.nodes:
        return f"{message}, which reports no nodes at all"

    close = difflib.get_close_matches(name, inventory.names, n=1)
    if close:
        message += f" (did you mean {close[0]!r}?)"

    listed = ", ".join(inventory.names[:_LISTED_NODES])
    remainder = len(inventory.names) - _LISTED_NODES
    if remainder > 0:
        listed += f" and {remainder} more"
    return f"{message}. The cluster's nodes are: {listed}"
