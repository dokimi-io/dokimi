"""The canonical model enforces the criterion rules, before anything is deployed."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from dokimi.model import (
    Bandwidth,
    Comparison,
    ConfigError,
    Criterion,
    Duration,
    Metric,
    Protocol,
    Statistic,
    TestDefinition,
    TestType,
    Unit,
)


def test_a_unit_of_the_wrong_dimension_is_rejected() -> None:
    # "throughput in microseconds" is the mistake this check exists for: a unit that is
    # spelled correctly but measures the wrong kind of thing.
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
    # 10**3 per step, not 1024: networking throughput is decimal.
    assert Bandwidth.parse(written).bits_per_second == Decimal(bits)


def test_bandwidth_renders_with_the_largest_exact_prefix() -> None:
    assert str(Bandwidth.parse("1G")) == "1G"
    assert str(Bandwidth.parse("1500M")) == "1500M"
    assert str(Bandwidth.parse("1001")) == "1001"


def test_the_reserved_scope_field_exists_but_stays_unset() -> None:
    # Scoping a criterion to part of the matrix is deferred to Milestone-02. The field
    # is present so the record does not change shape when it arrives.
    criterion = Criterion(
        metric=Metric.PACKET_LOSS,
        comparison=Comparison.AT_MOST,
        threshold=Decimal(0),
        unit=Unit.PERCENT,
    )
    assert criterion.scope is None


# -- values every renderer must be able to carry (review of #26) --------------


def _valid() -> TestDefinition:
    return TestDefinition(
        name="worker-01-to-worker-02-udp",
        source_node="worker-01",
        target_node="worker-02",
        test_type=TestType.IPERF2,
        protocol=Protocol.UDP,
        duration=Duration.parse("30s"),
        target_bandwidth=Bandwidth.parse("1G"),
        criteria=(
            Criterion(
                metric=Metric.THROUGHPUT,
                comparison=Comparison.AT_LEAST,
                threshold=Decimal(950),
                unit=Unit.MEGABITS_PER_SECOND,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("x\n    Log    injected", "control character", id="newline injects a keyword"),
        pytest.param("tab\there", "control character", id="tab"),
        pytest.param("two  spaces", "two spaces in a row", id="splits a Robot cell"),
        pytest.param("a | b", "contains '|'", id="pipe separator"),
        pytest.param("${TEST_TYPE}", "must start with a letter", id="Robot variable"),
        pytest.param("run ${X}", r"contains '\$'", id="Robot variable mid-name"),
        pytest.param("# hidden", "must start with a letter", id="Robot comment"),
        pytest.param("...continued", "must start with a letter", id="Robot continuation"),
        pytest.param("back\\slash", "contains", id="Robot escape"),
        pytest.param(" padded", "whitespace", id="leading space"),
        pytest.param("padded ", "whitespace", id="trailing space"),
    ],
)
def test_a_name_the_renderers_cannot_carry_is_rejected(name: str, expected: str) -> None:
    # Each of these was either an injection into the executed suite or a document that
    # does not parse back. Checked once, here, rather than escaped per renderer.
    with pytest.raises(ConfigError, match=expected):
        replace(_valid(), name=name)


@pytest.mark.parametrize(
    "name", ["worker-01-to-worker-02-udp", "Worker 01 to 02 (UDP)", "a: b", "run/2026.09"]
)
def test_ordinary_names_are_accepted(name: str) -> None:
    assert replace(_valid(), name=name).name == name


@pytest.mark.parametrize(
    "node", ["Bad Node!", "UPPERCASE", "-leading-hyphen", "trailing-", "has space", "a" * 254]
)
def test_a_node_name_kubernetes_would_not_accept_is_rejected(node: str) -> None:
    with pytest.raises(ConfigError, match="not a valid Kubernetes node name"):
        replace(_valid(), source_node=node)


def test_a_dotted_node_name_is_accepted() -> None:
    # Cloud providers name nodes by their DNS name, which is a DNS-1123 subdomain.
    node = "ip-10-0-1-5.eu-west-1.compute.internal"

    assert replace(_valid(), source_node=node).source_node == node


@pytest.mark.parametrize("directory", ["${HOME}/results", "results\nmore", "a  b"])
def test_a_results_directory_the_renderers_cannot_carry_is_rejected(directory: str) -> None:
    with pytest.raises(ConfigError):
        replace(_valid(), results_directory=directory)


@pytest.mark.parametrize("directory", ["./results", "results", "~/runs/m1", "/var/lib/runs"])
def test_ordinary_results_directories_are_accepted(directory: str) -> None:
    assert replace(_valid(), results_directory=directory).results_directory == directory


@pytest.mark.parametrize("threshold", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_non_finite_threshold_is_a_config_error(threshold: str) -> None:
    # NaN compares false against everything and infinity satisfies or fails every bound,
    # so neither is a threshold. Previously NaN surfaced later as InvalidOperation.
    with pytest.raises(ConfigError, match="must be a finite number"):
        Criterion(
            metric=Metric.THROUGHPUT,
            comparison=Comparison.AT_LEAST,
            threshold=Decimal(threshold),
            unit=Unit.MEGABITS_PER_SECOND,
        )
