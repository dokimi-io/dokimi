"""Node resolution, matrix construction and the concurrency plan.

Phase 3. Discovery reads the cluster's nodes once (:mod:`~dokimi.planning.nodes`)
and planning resolves the definition against that snapshot
(:mod:`~dokimi.planning.plan`). Bounded concurrency with node exclusivity is
ADR-04-04's default and the scheduler stays here in Python rather than inside Robot's
execution model (ADR-04-01, ADR-04-04); Milestone-01 plans one explicit pair,
sequentially.
"""

from __future__ import annotations

from dokimi.planning.nodes import NodeFacts, NodeInventory, NodeSource, Taint, discover
from dokimi.planning.plan import (
    PLACEMENT_MODE,
    Concurrency,
    Plan,
    PlannedPair,
    plan_run,
    resolve_node,
)

__all__ = [
    "PLACEMENT_MODE",
    "Concurrency",
    "NodeFacts",
    "NodeInventory",
    "NodeSource",
    "Plan",
    "PlannedPair",
    "Taint",
    "discover",
    "plan_run",
    "resolve_node",
]
