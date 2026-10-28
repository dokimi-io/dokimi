"""Phase 8: criteria evaluated over the expected pairs, from what phase 7 read.

The three acceptance criteria of M1-13 are the first three tests: a threshold in the
user's unit against a metric in its base unit, a missing series as an error rather than a
pass, and a failure message that names the pair and its value.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

import pytest

from dokimi.error_categories import ErrorCategory
from dokimi.measurement import Measurement
from dokimi.model import Comparison, Criterion, Metric, Statistic, Unit
from dokimi.validation import (
    MAX_LISTED,
    ExpectedPair,
    Outcome,
    TestError,
    TestOutcome,
    evaluate,
    format_value,
    outcome_of,
    phrase,
    promql_equivalent,
    read_outcome,
    read_validation,
    run_outcome,
    worst,
    write_validation,
)

THROUGHPUT = "dokimi_throughput_bits_per_second"
PACKET_LOSS = "dokimi_packet_loss_percent"
PAIR = ExpectedPair("pair-001", "worker-01", "worker-02")


def criterion(
    metric: Metric = Metric.THROUGHPUT,
    comparison: Comparison = Comparison.AT_LEAST,
    threshold: str = "950",
    unit: Unit = Unit.MEGABITS_PER_SECOND,
    statistic: Statistic | None = None,
) -> Criterion:
    return Criterion(metric, comparison, Decimal(threshold), unit, statistic)


def measured(test_id: str = "pair-001", protocol: str = "udp", **values: float) -> Measurement:
    series = {f"dokimi_{name}": value for name, value in values.items()}
    labels = {"test_id": test_id, "test_type": "iperf2", "protocol": protocol}
    return Measurement(test_id, "inbound", series, labels)


def pairs(count: int) -> list[ExpectedPair]:
    return [
        ExpectedPair(f"pair-{n:03d}", f"node-{n}", f"node-{n + 1}") for n in range(1, count + 1)
    ]


# -- the acceptance criteria ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("bits_per_second", "outcome"),
    [
        (950_000_000.0, Outcome.PASS),
        (949_999_999.0, Outcome.FAIL),
        (1_000_000_000.0, Outcome.PASS),
        (49_983_531.0, Outcome.FAIL),
    ],
)
def test_950_megabits_per_second_against_a_metric_in_bits_per_second(
    bits_per_second: float, outcome: Outcome
) -> None:
    result = evaluate(
        criterion(),
        {"pair-001": measured(throughput_bits_per_second=bits_per_second)},
        [PAIR],
    )

    assert result.outcome is outcome


def test_a_missing_series_is_an_error_never_a_pass() -> None:
    # The trap the rule exists for: "packet loss at most 0" over nothing is trivially true.
    loss = criterion(Metric.PACKET_LOSS, Comparison.AT_MOST, "0", Unit.PERCENT)

    result = evaluate(loss, {"pair-001": measured(throughput_bits_per_second=1e9)}, [PAIR])

    assert result.outcome is Outcome.ERROR
    assert result.categories == ("metrics_missing",)
    assert result.pairs[0].detail == f"its result has no {PACKET_LOSS}"


def test_no_measurement_at_all_is_an_error_never_a_pass() -> None:
    result = evaluate(criterion(), {}, [PAIR])

    assert result.outcome is Outcome.ERROR
    assert result.categories == ("metrics_missing",)
    assert "ERROR (metrics_missing)" in result.message()


def test_a_failure_message_names_the_pair_and_its_measured_value() -> None:
    result = evaluate(
        criterion(), {"pair-001": measured(throughput_bits_per_second=49_983_531.0)}, [PAIR]
    )

    assert result.message() == (
        "Throughput Should Be At Least 950 megabits per second: 1 of 1 pair(s) violated "
        "this criterion\n"
        "  worker-01 -> worker-02 (pair-001): 49.98353 megabits per second, needs at least 950"
    )


# -- units ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "threshold"),
    [
        (Unit.BITS_PER_SECOND, "950000000"),
        (Unit.KILOBITS_PER_SECOND, "950000"),
        (Unit.MEGABITS_PER_SECOND, "950"),
        (Unit.GIGABITS_PER_SECOND, "0.95"),
    ],
)
def test_throughput_prefixes_are_decimal(unit: Unit, threshold: str) -> None:
    # The same bound in every unit: met exactly, and missed by one bit per second.
    at = {"pair-001": measured(throughput_bits_per_second=950_000_000.0)}
    below = {"pair-001": measured(throughput_bits_per_second=949_999_999.0)}

    assert evaluate(criterion(unit=unit, threshold=threshold), at, [PAIR]).outcome is Outcome.PASS
    assert (
        evaluate(criterion(unit=unit, threshold=threshold), below, [PAIR]).outcome is Outcome.FAIL
    )


def test_a_value_is_compared_in_the_users_unit_without_binary_noise() -> None:
    # 0.1 + 0.2 as a float is 0.30000000000000004: read through repr(), it is still more
    # than 0.3, and the comparison says so rather than rounding it away.
    loss = criterion(Metric.PACKET_LOSS, Comparison.AT_MOST, "0.3", Unit.PERCENT)

    result = evaluate(loss, {"pair-001": measured(packet_loss_percent=0.1 + 0.2)}, [PAIR])

    assert result.outcome is Outcome.FAIL
    assert result.pairs[0].value == Decimal("0.30000000000000004")


def test_jitter_in_microseconds_against_a_metric_in_seconds() -> None:
    jitter = criterion(Metric.JITTER, Comparison.AT_MOST, "20", Unit.MICROSECONDS)

    passed = evaluate(jitter, {"pair-001": measured(jitter_seconds=1.1e-05)}, [PAIR])
    failed = evaluate(jitter, {"pair-001": measured(jitter_seconds=2.5e-05)}, [PAIR])

    assert passed.outcome is Outcome.PASS
    assert failed.outcome is Outcome.FAIL
    assert "25 microseconds, needs at most 20" in failed.message()


# -- comparisons ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("comparison", "loss", "outcome"),
    [
        (Comparison.AT_MOST, 0.0, Outcome.PASS),
        (Comparison.AT_MOST, 0.39, Outcome.FAIL),
        (Comparison.AT_LEAST, 0.39, Outcome.PASS),
        (Comparison.EXACTLY, 0.0, Outcome.PASS),
        (Comparison.EXACTLY, 0.39, Outcome.FAIL),
    ],
)
def test_each_comparison_bounds_the_value_the_way_it_reads(
    comparison: Comparison, loss: float, outcome: Outcome
) -> None:
    rule = criterion(Metric.PACKET_LOSS, comparison, "0", Unit.PERCENT)

    assert (
        evaluate(rule, {"pair-001": measured(packet_loss_percent=loss)}, [PAIR]).outcome is outcome
    )


# -- what is not a measurement --------------------------------------------------------------


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_value_that_is_not_a_finite_number_is_exporter_parse(value: float) -> None:
    result = evaluate(criterion(), {"pair-001": measured(throughput_bits_per_second=value)}, [PAIR])

    assert result.outcome is Outcome.ERROR
    assert result.categories == ("exporter_parse",)


def test_a_metric_the_test_type_never_produces_is_reported_once() -> None:
    # iperf2 over TCP has no packet loss. Three pairs, one line, not three identical ones.
    expected = pairs(3)
    loss = criterion(Metric.PACKET_LOSS, Comparison.AT_MOST, "0", Unit.PERCENT)
    measurements = {
        pair.test_id: measured(pair.test_id, "tcp", throughput_bits_per_second=1e9)
        for pair in expected
    }

    result = evaluate(loss, measurements, expected)

    assert result.outcome is Outcome.ERROR
    assert result.not_produced == (
        "iperf2 over tcp does not produce packetLoss, so no pair can be checked against it"
    )
    assert result.message().count("does not produce") == 1
    assert "pair-002" not in result.message()


def test_latency_is_produced_by_no_test_type_yet() -> None:
    latency = criterion(Metric.LATENCY, Comparison.AT_MOST, "10", Unit.MICROSECONDS, Statistic.MAX)

    result = evaluate(latency, {"pair-001": measured(throughput_bits_per_second=1e9)}, [PAIR])

    assert result.outcome is Outcome.ERROR
    assert result.not_produced is not None
    assert "iperf2 over udp does not produce latency" in result.not_produced


def test_a_missing_series_without_a_protocol_is_missing_per_pair_not_never_produced() -> None:
    # Without the labels to tell, nothing is concluded about the test type.
    unlabelled = Measurement("pair-001", "inbound", {THROUGHPUT: 1e9}, {})
    loss = criterion(Metric.PACKET_LOSS, Comparison.AT_MOST, "0", Unit.PERCENT)

    result = evaluate(loss, {"pair-001": unlabelled}, [PAIR])

    assert result.not_produced is None
    assert result.categories == ("metrics_missing",)


def test_the_expected_pairs_come_from_the_plan_not_from_what_came_back() -> None:
    # Two pairs expected, one measured, and it passes: the criterion still doesn't.
    expected = pairs(2)
    measurements = {"pair-001": measured(throughput_bits_per_second=1e9)}

    result = evaluate(criterion(), measurements, expected)

    assert result.outcome is Outcome.ERROR
    assert [r.outcome for r in result.pairs] == [Outcome.PASS, Outcome.ERROR]
    assert "1 of 2 pair(s) could not be evaluated" in result.message()


def test_a_measurement_nobody_expected_is_not_evaluated() -> None:
    result = evaluate(
        criterion(),
        {
            "pair-001": measured(throughput_bits_per_second=1e9),
            "pair-999": measured("pair-999", throughput_bits_per_second=1.0),
        },
        [PAIR],
    )

    assert result.outcome is Outcome.PASS
    assert [r.pair.test_id for r in result.pairs] == ["pair-001"]


def test_nothing_to_quantify_over_is_refused_rather_than_passed() -> None:
    with pytest.raises(ValueError, match="at least one expected pair"):
        evaluate(criterion(), {}, [])


# -- the message ----------------------------------------------------------------------------


def test_an_error_outranks_a_failure_and_the_two_are_listed_apart() -> None:
    expected = pairs(3)
    measurements = {
        "pair-001": measured("pair-001", throughput_bits_per_second=1e9),
        "pair-002": measured("pair-002", throughput_bits_per_second=5e8),
    }

    result = evaluate(criterion(), measurements, expected)

    assert result.outcome is Outcome.ERROR
    lines = result.message().splitlines()
    # The errors first, marked as such, then the failures under their own count.
    assert lines[0] == (
        "ERROR (metrics_missing): Throughput Should Be At Least 950 megabits per second: "
        "1 of 3 pair(s) could not be evaluated. This is not a finding about the network"
    )
    assert lines[1].startswith("  node-3 -> node-4 (pair-003): metrics_missing: no measurement")
    assert lines[2].endswith("1 of 3 pair(s) violated this criterion")
    assert lines[3].startswith("  node-2 -> node-3 (pair-002): 500 megabits per second")


def test_the_worst_pairs_come_first_and_the_list_stops_at_ten() -> None:
    expected = pairs(12)
    measurements = {
        pair.test_id: measured(pair.test_id, throughput_bits_per_second=(n + 1) * 1e6)
        for n, pair in enumerate(expected)
    }

    lines = evaluate(criterion(), measurements, expected).message().splitlines()

    assert lines[0].endswith("12 of 12 pair(s) violated this criterion")
    assert lines[1].startswith("  node-1 -> node-2 (pair-001): 1 megabits per second")
    assert lines[MAX_LISTED].startswith("  node-10 -> node-11 (pair-010)")
    assert lines[MAX_LISTED + 1] == "  ... and 2 more, in each test's validation.json"
    assert len(lines) == MAX_LISTED + 2


def test_a_pass_says_which_pair_came_closest() -> None:
    expected = pairs(2)
    measurements = {
        "pair-001": measured("pair-001", throughput_bits_per_second=9.6e8),
        "pair-002": measured("pair-002", throughput_bits_per_second=9.9e8),
    }

    result = evaluate(criterion(), measurements, expected)

    assert result.message() == (
        "Throughput Should Be At Least 950 megabits per second: holds for all 2 pair(s), "
        "the closest being node-1 -> node-2 (pair-001) at 960 megabits per second"
    )


def test_the_criterion_reads_as_the_suite_wrote_it() -> None:
    written = "Throughput Should Be    At Least    950    megabits per second"
    result = evaluate(
        criterion(), {"pair-001": measured(throughput_bits_per_second=1e9)}, [PAIR], written=written
    )

    assert result.phrase == written
    assert phrase(criterion()) == "Throughput Should Be At Least 950 megabits per second"
    assert (
        phrase(
            criterion(Metric.LATENCY, Comparison.AT_MOST, "10", Unit.MICROSECONDS, Statistic.MAX)
        )
        == "Maximum Latency Should Be At Most 10 microseconds"
    )


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("49.983531"), "49.98353"),
        (Decimal("0.00001"), "0.00001"),
        (Decimal("950"), "950"),
        (Decimal("0E-7"), "0"),
        (Decimal("1234567890"), "1234568000"),
    ],
)
def test_a_value_for_a_person_has_no_exponent(value: Decimal, text: str) -> None:
    assert format_value(value) == text


def test_a_value_one_bit_short_is_never_shown_as_the_threshold() -> None:
    # Seven digits would round 949.999999 to 950, and the message would say a pair at 950
    # failed "at least 950". It gets the digits it takes to stay below.
    result = evaluate(
        criterion(), {"pair-001": measured(throughput_bits_per_second=949_999_999.0)}, [PAIR]
    )

    assert result.outcome is Outcome.FAIL
    assert "(pair-001): 949.999999 megabits per second, needs at least 950" in result.message()


@pytest.mark.parametrize(
    ("value", "threshold", "text"),
    [
        (Decimal("949.999999"), Decimal("950"), "949.999999"),
        (Decimal("950.0000001"), Decimal("950"), "950.0000001"),
        (Decimal("950"), Decimal("950"), "950"),
        (Decimal("49.983531"), Decimal("950"), "49.98353"),
        # A threshold between the value and its seven-digit rounding.
        (Decimal("123.45678"), Decimal("123.456785"), "123.45678"),
    ],
)
def test_a_value_sits_on_the_same_side_of_the_threshold_as_it_really_does(
    value: Decimal, threshold: Decimal, text: str
) -> None:
    assert format_value(value, threshold=threshold) == text


# -- the PromQL equivalent ------------------------------------------------------------------


def test_the_promql_equivalent_returns_the_violating_pairs_in_base_units() -> None:
    assert promql_equivalent(criterion(), "20261002-120000-a1b2c3") == (
        'dokimi_throughput_bits_per_second{run_id="20261002-120000-a1b2c3",'
        'direction="inbound"} < 950000000'
    )
    assert promql_equivalent(
        criterion(Metric.PACKET_LOSS, Comparison.AT_MOST, "0", Unit.PERCENT)
    ) == ('dokimi_packet_loss_percent{direction="inbound"} > 0')


def test_the_promql_equivalent_keeps_the_threshold_exact() -> None:
    # Rounded, 123.4567891 megabits per second would be < 123456800, which no longer
    # reproduces the verdict.
    precise = criterion(threshold="123.4567891")

    assert promql_equivalent(precise).endswith(" < 123456789.1")
    assert promql_equivalent(
        criterion(Metric.JITTER, Comparison.AT_MOST, "10", Unit.MICROSECONDS)
    ).endswith(" > 0.00001")


def test_each_evaluation_carries_its_promql_equivalent() -> None:
    result = evaluate(
        criterion(),
        {"pair-001": measured(throughput_bits_per_second=1e9)},
        [PAIR],
        run_id="20261002-120000-a1b2c3",
    )

    assert result.promql_equivalent == promql_equivalent(criterion(), "20261002-120000-a1b2c3")


# -- the record the Job and M1-14/M1-16 read ------------------------------------------------


def test_a_tests_outcome_is_its_worst_criterion() -> None:
    measurement = {"pair-001": measured(throughput_bits_per_second=1e9, packet_loss_percent=1.0)}
    passing = evaluate(criterion(), measurement, [PAIR])
    failing = evaluate(
        criterion(Metric.PACKET_LOSS, Comparison.AT_MOST, "0", Unit.PERCENT), measurement, [PAIR]
    )
    erring = evaluate(criterion(), {}, [PAIR])

    # Nothing asserted is not a pass. Both formats refuse it; a hand-written suite can't.
    assert outcome_of([]) is Outcome.ERROR
    assert outcome_of([passing]) is Outcome.PASS
    assert outcome_of([passing, failing]) is Outcome.FAIL
    assert outcome_of([failing, erring, passing]) is Outcome.ERROR


def test_the_validation_record_round_trips_next_to_the_output(tmp_path: Path) -> None:
    result = evaluate(
        criterion(), {"pair-001": measured(throughput_bits_per_second=49_983_531.0)}, [PAIR]
    )

    path = write_validation("pair-001", [result], tmp_path)

    assert path == tmp_path / "pair-001.validation.json"
    document = read_validation(tmp_path, "pair-001")
    assert document is not None
    assert document == json.loads(path.read_text(encoding="utf-8"))
    assert document["schema"] == 1
    assert document["outcome"] == "fail"
    assert document["error"] is None
    (only,) = document["criteria"]
    assert only["phrase"] == "Throughput Should Be At Least 950 megabits per second"
    assert (only["metric"], only["comparison"], only["threshold"], only["unit"]) == (
        "throughput",
        "atLeast",
        "950",
        "megabits per second",
    )
    assert only["pairs"] == [
        {
            "test_id": "pair-001",
            "source_node": "worker-01",
            "target_node": "worker-02",
            "outcome": "fail",
            "value": "49.983531",
            "category": None,
            "detail": "",
        }
    ]
    assert only["promql_equivalent"].endswith("< 950000000")


def test_no_record_is_none(tmp_path: Path) -> None:
    assert read_validation(tmp_path, "pair-001") is None


# -- what a test, and the run, came to ---------------------------------------------------


def test_a_test_that_ended_before_its_criteria_is_recorded_as_an_error(tmp_path: Path) -> None:
    passing = evaluate(
        criterion(threshold="1"), {"pair-001": measured(throughput_bits_per_second=1e9)}, [PAIR]
    )
    error = TestError(ErrorCategory.TIMEOUT, "pair-001 did not complete within 75s")

    # Whatever was evaluated before it, the test did not get to the end: an error.
    write_validation("pair-001", [passing], tmp_path, error=error)

    test = read_outcome(tmp_path, "pair-001")
    assert test.outcome is Outcome.ERROR
    assert test.error == error
    assert test.document is not None
    assert test.document["error"] == {
        "category": "timeout",
        "detail": "pair-001 did not complete within 75s",
    }


def test_a_test_that_recorded_nothing_is_an_error(tmp_path: Path) -> None:
    test = read_outcome(tmp_path, "pair-001")

    assert test.outcome is Outcome.ERROR
    assert not test.recorded
    assert "ended before its outcome was recorded" in test.problem


@pytest.mark.parametrize(
    "content", ["{not json", '{"outcome": "maybe"}'], ids=["unreadable", "unknown outcome"]
)
def test_a_record_that_cant_be_read_is_an_error(tmp_path: Path, content: str) -> None:
    (tmp_path / "pair-001.validation.json").write_text(content, encoding="utf-8")

    test = read_outcome(tmp_path, "pair-001")

    assert test.outcome is Outcome.ERROR
    assert not test.recorded


def test_an_unknown_error_category_is_still_an_error_with_none(tmp_path: Path) -> None:
    write_validation("pair-001", [], tmp_path, error=TestError(None, "surprise"))

    test = read_outcome(tmp_path, "pair-001")

    assert test.outcome is Outcome.ERROR
    assert test.error == TestError(None, "surprise")


def outcome(value: Outcome) -> TestOutcome:
    return TestOutcome("pair-001", value, {"outcome": value.value})


def test_the_runs_outcome_is_its_worst_test() -> None:
    assert run_outcome([outcome(Outcome.PASS)], anomaly=False) is Outcome.PASS
    assert run_outcome([outcome(Outcome.PASS), outcome(Outcome.FAIL)], anomaly=False) is (
        Outcome.FAIL
    )
    # An error outranks a fail: part of the run was not validly measured.
    assert run_outcome([outcome(Outcome.ERROR), outcome(Outcome.FAIL)], anomaly=False) is (
        Outcome.ERROR
    )


def test_an_anomaly_or_no_test_at_all_makes_the_run_an_error() -> None:
    assert run_outcome([outcome(Outcome.PASS)], anomaly=True) is Outcome.ERROR
    assert run_outcome([outcome(Outcome.FAIL)], anomaly=True) is Outcome.ERROR
    assert run_outcome([], anomaly=False) is Outcome.ERROR
    assert worst([]) is Outcome.ERROR
