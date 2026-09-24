"""The canonical test definition: what one directed pair needs, and nothing more.

ADR-04-06 scopes Milestone-01 to exactly this: two explicitly named nodes, one test type,
one protocol, a duration, a target bandwidth and the pass criteria. Selectors, matrix
modes and anything producing more than one pair belong to ADR-04-01 and Milestone-02.

Durations and bandwidths are parsed into typed values rather than kept as strings, so a
malformed one is a ``config_error`` in phase 2 instead of a surprise in phase 6. Both
render back to a canonical spelling, which is what the round-trip property in ADR-04-05
asks for — model equivalence, not byte equality of the source.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from test_orchestrator.model.criterion import Criterion
from test_orchestrator.model.errors import ConfigError

__all__ = [
    "Bandwidth",
    "DEFAULT_RESULTS_DIRECTORY",
    "Duration",
    "MonitoringMode",
    "Protocol",
    "TestDefinition",
    "TestType",
]


class TestType(enum.Enum):
    """The test types the model can express. Milestone-01 runs ``iperf2`` only."""

    IPERF2 = "iperf2"

    @classmethod
    def parse(cls, text: str) -> TestType:
        for member in cls:
            if member.value == text:
                return member
        raise ConfigError(
            f"unknown test type {text!r}. Accepted test types are: "
            + ", ".join(repr(m.value) for m in cls)
        )


class MonitoringMode(enum.Enum):
    """Where the run's Prometheus comes from (ADR-03-02).

    ``auto`` is the default: prefer a compatible existing installation, fall back to
    deploying one for the run.
    """

    MANAGED = "managed"
    EXISTING = "existing"
    AUTO = "auto"

    @classmethod
    def parse(cls, text: str) -> MonitoringMode:
        for member in cls:
            if member.value == text:
                return member
        raise ConfigError(
            f"unknown monitoring mode {text!r}. Accepted modes are: "
            + ", ".join(repr(m.value) for m in cls)
        )


#: Where the local result artifact lands when the document does not say (ADR-05-09).
DEFAULT_RESULTS_DIRECTORY = "./results"


class Protocol(enum.Enum):
    UDP = "udp"
    TCP = "tcp"

    @classmethod
    def parse(cls, text: str) -> Protocol:
        for member in cls:
            if member.value == text.lower():
                return member
        raise ConfigError(
            f"unknown protocol {text!r}. Accepted protocols are: "
            + ", ".join(repr(m.value) for m in cls)
        )


_DURATION = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<suffix>ms|s|m|h)$")
_DURATION_SECONDS: dict[str, Decimal] = {
    "ms": Decimal("0.001"),
    "s": Decimal(1),
    "m": Decimal(60),
    "h": Decimal(3600),
}


@dataclass(frozen=True, slots=True)
class Duration:
    """How long the test runs, held in seconds."""

    seconds: Decimal

    @classmethod
    def parse(cls, text: str) -> Duration:
        match = _DURATION.match(str(text).strip())
        if match is None:
            raise ConfigError(
                f"malformed duration {text!r}. Expected a number followed by "
                "'ms', 's', 'm' or 'h', for example '30s'"
            )
        try:
            value = Decimal(match.group("value"))
        except InvalidOperation as exc:  # pragma: no cover - regex already constrains it
            raise ConfigError(f"malformed duration {text!r}") from exc
        if value <= 0:
            raise ConfigError(f"duration must be greater than zero, got {text!r}")
        return cls(seconds=value * _DURATION_SECONDS[match.group("suffix")])

    def __str__(self) -> str:
        """Canonical spelling, always in seconds."""
        return f"{_plain(self.seconds)}s"


_BANDWIDTH = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<suffix>[KMG]?)$")
_BANDWIDTH_BITS: dict[str, Decimal] = {
    "": Decimal(1),
    "K": Decimal(10) ** 3,
    "M": Decimal(10) ** 6,
    "G": Decimal(10) ** 9,
}


@dataclass(frozen=True, slots=True)
class Bandwidth:
    """The rate the test aims at, held in bits per second.

    Prefixes are decimal, 10³ per step, matching ADR-02-03's rule for throughput. A
    reader may assume binary prefixes here, which is exactly why that ADR states it.
    """

    bits_per_second: Decimal

    @classmethod
    def parse(cls, text: str) -> Bandwidth:
        match = _BANDWIDTH.match(str(text).strip())
        if match is None:
            raise ConfigError(
                f"malformed bandwidth {text!r}. Expected a number with an optional "
                "'K', 'M' or 'G' suffix, for example '1G'"
            )
        value = Decimal(match.group("value"))
        if value <= 0:
            raise ConfigError(f"bandwidth must be greater than zero, got {text!r}")
        return cls(bits_per_second=value * _BANDWIDTH_BITS[match.group("suffix")])

    def __str__(self) -> str:
        """Canonical spelling, using the largest decimal prefix that stays exact."""
        for suffix in ("G", "M", "K"):
            factor = _BANDWIDTH_BITS[suffix]
            scaled = self.bits_per_second / factor
            if scaled >= 1 and scaled == scaled.to_integral_value():
                return f"{_plain(scaled)}{suffix}"
        return _plain(self.bits_per_second)


@dataclass(frozen=True, slots=True)
class TestDefinition:
    """One directed test between two explicitly named nodes."""

    name: str
    source_node: str
    target_node: str
    test_type: TestType
    protocol: Protocol
    duration: Duration
    target_bandwidth: Bandwidth
    criteria: tuple[Criterion, ...] = field(default=())

    #: Both sections below are part of a Milestone-01 run's configuration, and
    #: ADR-04-06 makes the test document the only source of it. They carry defaults
    #: because the decisions behind them already have one: ADR-03-02 makes ``auto`` the
    #: default monitoring mode, and ADR-05-09 requires a result artifact unconditionally,
    #: so a run with a silent document still has somewhere to write.
    monitoring_mode: MonitoringMode = MonitoringMode.AUTO
    results_directory: str = DEFAULT_RESULTS_DIRECTORY

    def __post_init__(self) -> None:
        for label, value in (
            ("name", self.name),
            ("source node", self.source_node),
            ("target node", self.target_node),
        ):
            if not value or not value.strip():
                raise ConfigError(f"{label} must not be empty")
        if self.source_node == self.target_node:
            raise ConfigError(
                f"source and target node are both {self.source_node!r}; a directed test "
                "needs two distinct nodes"
            )
        if not self.criteria:
            raise ConfigError(
                "a test definition needs at least one pass criterion, otherwise the run "
                "has nothing to report a verdict against"
            )


def _plain(value: Decimal) -> str:
    """Render a Decimal without an exponent or trailing zeros."""
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal(1))
    return format(normalised, "f")
