"""The canonical model enforces what ADR-02-05 decided, before anything is deployed."""

from __future__ import annotations

from decimal import Decimal

import pytest

from test_orchestrator.model import (
    Bandwidth,
    Comparison,
    ConfigError,
    Criterion,
    Duration,
    Metric,
    Statistic,
    Unit,
)


def test_a_unit_of_the_wrong_dimension_is_rejected() -> None:
    # "throughput in microseconds" is the mistake this check exists for: a unit that is
    # spelled correctly but measures the wrong kind of thing (ADR-02-05).
    with pytest.raises(ConfigError, match="does not measure throughput"):
        Criterion(
            metric=Metric.THROUGHPUT,
            comparison=Comparison.AT_LEAST,
            threshold=Decimal(950),
            unit=Unit.MICROSECONDS,
        )


def test_an_unknown_unit_names_the_accepted_ones() -> None:
    with pytest.raises(ConfigError, match="megabits per second"):
        Unit.parse("megabits/s")


def test_latency_requires_a_statistic_and_the_others_refuse_one() -> None:
    with pytest.raises(ConfigError, match="requires a statistic"):
        Criterion(
            metric=Metric.LATENCY,
            comparison=Comparison.AT_MOST,
            threshold=Decimal(10),
            unit=Unit.MICROSECONDS,
        )

    with pytest.raises(ConfigError, match="does not carry a statistic"):
        Criterion(
            metric=Metric.THROUGHPUT,
            comparison=Comparison.AT_LEAST,
            threshold=Decimal(950),
            unit=Unit.MEGABITS_PER_SECOND,
            statistic=Statistic.MAX,
        )


@pytest.mark.parametrize(
    ("written", "seconds"),
    [("30s", 30), ("500ms", Decimal("0.5")), ("5m", 300), ("1h", 3600)],
)
def test_durations_parse_to_seconds(written: str, seconds: object) -> None:
    assert Duration.parse(written).seconds == Decimal(str(seconds))


@pytest.mark.parametrize("written", ["30", "30 s", "-5s", "0s", "thirty seconds"])
def test_malformed_durations_are_config_errors(written: str) -> None:
    with pytest.raises(ConfigError):
        Duration.parse(written)


@pytest.mark.parametrize(
    ("written", "bits"),
    [("1G", 10**9), ("950M", 950 * 10**6), ("1K", 1000), ("1000", 1000)],
)
def test_bandwidth_prefixes_are_decimal(written: str, bits: int) -> None:
    # 10**3 per step, not 1024: networking throughput is decimal (ADR-02-03).
    assert Bandwidth.parse(written).bits_per_second == Decimal(bits)


def test_bandwidth_renders_with_the_largest_exact_prefix() -> None:
    assert str(Bandwidth.parse("1G")) == "1G"
    assert str(Bandwidth.parse("1500M")) == "1500M"
    assert str(Bandwidth.parse("1001")) == "1001"


def test_the_reserved_scope_field_exists_but_stays_unset() -> None:
    # OQ-32 deferred criterion scoping to Milestone-02. The field is present so the
    # record does not change shape when it arrives.
    criterion = Criterion(
        metric=Metric.PACKET_LOSS,
        comparison=Comparison.AT_MOST,
        threshold=Decimal(0),
        unit=Unit.PERCENT,
    )
    assert criterion.scope is None
