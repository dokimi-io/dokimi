"""Canonical test definition model.

The only representation the orchestration library consumes: parameters and pass criteria
together. Phase 2 produces it; every later phase reads it.

Milestone-01's subset is scoped to one directed pair between two explicitly
named nodes. Nothing here knows how a definition was authored, which is the point: the
formats depend on this package, never the reverse.
"""

from __future__ import annotations

from dokimi.model.criterion import Comparison, Criterion, Metric, Statistic
from dokimi.model.definition import (
    DEFAULT_RESULTS_DIRECTORY,
    Bandwidth,
    Duration,
    MonitoringMode,
    Protocol,
    TestDefinition,
    TestType,
)
from dokimi.model.errors import ConfigError
from dokimi.model.units import Dimension, Unit

__all__ = [
    "DEFAULT_RESULTS_DIRECTORY",
    "Bandwidth",
    "Comparison",
    "ConfigError",
    "Criterion",
    "Dimension",
    "Duration",
    "Metric",
    "MonitoringMode",
    "Protocol",
    "Statistic",
    "TestDefinition",
    "TestType",
    "Unit",
]
