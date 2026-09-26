"""The canonical criterion record.

ADR-02-05 (OQ-30) decided that a pass criterion is **one record** that every authoring
format renders, rather than a phrasing in one format that the others approximate:
``metric``, optional ``statistic``, ``comparison``, ``threshold``, ``unit``, and a
reserved ``scope``.

Two rules from that decision are enforced here rather than left to the formats, because
enforcing them per format is how the formats drift apart:

* the unit must belong to the metric's dimension, and
* ``latency`` requires a statistic while the others must not carry one.

Both are ``config_error`` (ADR-05-05), raised before anything is deployed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from dokimi.model.errors import ConfigError
from dokimi.model.units import Dimension, Unit

__all__ = ["Comparison", "Criterion", "Metric", "Statistic"]


class Comparison(enum.Enum):
    """The bound a criterion asserts. Always explicit, never implied by the metric."""

    AT_LEAST = "atLeast"
    AT_MOST = "atMost"
    EXACTLY = "exactly"

    @property
    def robot_phrasing(self) -> str:
        return _COMPARISON_PHRASING[self]


class Statistic(enum.Enum):
    """Which view of a measured quantity a criterion asserts over.

    ADR-02-04 makes min/avg/max a ``stat`` label rather than three metric names, because
    they are three views of one measured quantity rather than different quantities.
    """

    MIN = "min"
    AVG = "avg"
    MAX = "max"

    @property
    def robot_phrasing(self) -> str:
        return self.value.capitalize() if self is not Statistic.MAX else "Maximum"


class Metric(enum.Enum):
    """The logical criterion name. The user never writes the underlying metric name."""

    THROUGHPUT = "throughput"
    PACKET_LOSS = "packetLoss"
    JITTER = "jitter"
    LATENCY = "latency"

    @property
    def dimension(self) -> Dimension:
        return _METRIC_DIMENSIONS[self]

    @property
    def requires_statistic(self) -> bool:
        """Only ``latency`` carries a ``stat`` label, so only it takes a statistic."""
        return self is Metric.LATENCY

    @property
    def robot_phrasing(self) -> str:
        return _METRIC_PHRASING[self]

    @classmethod
    def parse(cls, text: str) -> Metric:
        for metric in cls:
            if metric.value == text:
                return metric
        raise ConfigError(
            f"unknown metric {text!r}. Accepted metrics are: "
            + ", ".join(repr(m.value) for m in cls)
        )


@dataclass(frozen=True, slots=True)
class Criterion:
    """One pass criterion, in the form every authoring format renders.

    ``threshold`` is a :class:`~decimal.Decimal` so that the number the user wrote
    survives a round trip unchanged. A float would turn ``0`` into ``0.0`` and invite
    binary-representation noise into a value that appears in a failure message.
    """

    metric: Metric
    comparison: Comparison
    threshold: Decimal
    unit: Unit
    statistic: Statistic | None = None

    #: Reserved by ADR-02-05 for scoping a criterion to part of the matrix
    #: (OQ-32, deferred to Milestone-02). Present so the record does not change shape
    #: when it arrives; must be ``None`` until then.
    scope: None = None

    def __post_init__(self) -> None:
        if not self.threshold.is_finite():
            # NaN compares false against everything and infinity passes or fails every
            # bound, so neither is a threshold. Decimal accepts both spellings silently.
            raise ConfigError(
                f"threshold for {self.metric.value!r} must be a finite number, got {self.threshold}"
            )
        if self.unit.dimension is not self.metric.dimension:
            raise ConfigError(
                f"unit {self.unit.value!r} does not measure "
                f"{self.metric.dimension.value} and cannot be used with metric "
                f"{self.metric.value!r}"
            )
        if self.metric.requires_statistic and self.statistic is None:
            raise ConfigError(
                f"metric {self.metric.value!r} requires a statistic "
                "(one of: " + ", ".join(repr(s.value) for s in Statistic) + ")"
            )
        if not self.metric.requires_statistic and self.statistic is not None:
            raise ConfigError(
                f"metric {self.metric.value!r} does not carry a statistic, "
                f"but {self.statistic.value!r} was given"
            )


_COMPARISON_PHRASING: dict[Comparison, str] = {
    Comparison.AT_LEAST: "At Least",
    Comparison.AT_MOST: "At Most",
    Comparison.EXACTLY: "Exactly",
}

_METRIC_PHRASING: dict[Metric, str] = {
    Metric.THROUGHPUT: "Throughput",
    Metric.PACKET_LOSS: "Packet Loss",
    Metric.JITTER: "Jitter",
    Metric.LATENCY: "Latency",
}

_METRIC_DIMENSIONS: dict[Metric, Dimension] = {
    Metric.THROUGHPUT: Dimension.THROUGHPUT,
    Metric.PACKET_LOSS: Dimension.PROPORTION,
    Metric.JITTER: Dimension.TIME,
    Metric.LATENCY: Dimension.TIME,
}
