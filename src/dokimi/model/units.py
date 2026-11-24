"""Units a criterion may be written in, and the dimensions they belong to.

The accepted spellings are fixed, and one rule shapes this whole module: **the
unit is a required argument, never defaulted**, because a defaulted unit is how a
factor-of-1000 error passes review.

A unit carries its dimension so that a criterion can reject a unit of the wrong *kind*
("throughput in microseconds") rather than only an unspelled one. Both are a
``config_error`` raised before deployment.

Throughput prefixes are **decimal**, 10³ per step — stated explicitly here
because a reader may assume binary prefixes, while networking throughput is decimal.

The conversion factors are recorded here but not applied here. Conversion happens
at the edges: the validation layer, :mod:`dokimi.validation`, converts on read. The model keeps
the value and the unit the user wrote, which is also what lets ``render_robot`` reproduce
the user's own words.
"""

from __future__ import annotations

import enum
from decimal import Decimal

__all__ = ["Dimension", "Unit"]


class Dimension(enum.Enum):
    """What kind of quantity a unit measures."""

    THROUGHPUT = "throughput"
    PROPORTION = "proportion"
    TIME = "time"


class Unit(enum.Enum):
    """An accepted unit spelling.

    The value is the canonical spelling, and it is identical in both authoring formats.
    That is deliberate: every criterion must be expressible in either
    format, and a unit that is spelled differently in each is one more thing that can
    drift apart.
    """

    BITS_PER_SECOND = "bits per second"
    KILOBITS_PER_SECOND = "kilobits per second"
    MEGABITS_PER_SECOND = "megabits per second"
    GIGABITS_PER_SECOND = "gigabits per second"

    PERCENT = "percent"

    SECONDS = "seconds"
    MILLISECONDS = "milliseconds"
    MICROSECONDS = "microseconds"
    NANOSECONDS = "nanoseconds"

    @property
    def dimension(self) -> Dimension:
        return _DIMENSIONS[self]

    @property
    def base_factor(self) -> Decimal:
        """Multiplier to this unit's base unit: bits per second, percent, or seconds.

        Applied by the validation layer; nothing in the model applies it.
        """
        return _BASE_FACTORS[self]

    @classmethod
    def parse(cls, text: str) -> Unit:
        """Resolve a written unit, or raise ``ConfigError``.

        Case and surrounding whitespace are forgiven; spelling is not. The accepted
        spellings are listed exactly, and inventing synonyms here would create two names for
        one number.
        """
        from dokimi.model.errors import ConfigError

        normalised = " ".join(text.strip().lower().split())
        for unit in cls:
            if unit.value == normalised:
                return unit
        raise ConfigError(
            f"unknown unit {text!r}. Accepted units are: " + ", ".join(repr(u.value) for u in cls)
        )

    def __str__(self) -> str:
        return self.value


_DIMENSIONS: dict[Unit, Dimension] = {
    Unit.BITS_PER_SECOND: Dimension.THROUGHPUT,
    Unit.KILOBITS_PER_SECOND: Dimension.THROUGHPUT,
    Unit.MEGABITS_PER_SECOND: Dimension.THROUGHPUT,
    Unit.GIGABITS_PER_SECOND: Dimension.THROUGHPUT,
    Unit.PERCENT: Dimension.PROPORTION,
    Unit.SECONDS: Dimension.TIME,
    Unit.MILLISECONDS: Dimension.TIME,
    Unit.MICROSECONDS: Dimension.TIME,
    Unit.NANOSECONDS: Dimension.TIME,
}

# Decimal prefixes, 10**3 per step.
_BASE_FACTORS: dict[Unit, Decimal] = {
    Unit.BITS_PER_SECOND: Decimal(1),
    Unit.KILOBITS_PER_SECOND: Decimal(10) ** 3,
    Unit.MEGABITS_PER_SECOND: Decimal(10) ** 6,
    Unit.GIGABITS_PER_SECOND: Decimal(10) ** 9,
    Unit.PERCENT: Decimal(1),
    Unit.SECONDS: Decimal(1),
    Unit.MILLISECONDS: Decimal(10) ** -3,
    Unit.MICROSECONDS: Decimal(10) ** -6,
    Unit.NANOSECONDS: Decimal(10) ** -9,
}
