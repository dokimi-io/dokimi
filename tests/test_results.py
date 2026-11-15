"""Phase 9b: the result files, at a test's grain and at an assertion's, as CSV and JSONL.

The acceptance criteria of M1-14 that live here: both files are written, every assertion
is in them, passing ones included, and their values are the ones the verdict was computed
from. The records are built from what the tests themselves wrote, as the Job reads it.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from dokimi.error_categories import ErrorCategory
from dokimi.formats import parse_file
from dokimi.measurement import Measurement, write_measurement
from dokimi.planning import Plan, PlannedPair
from dokimi.planning.nodes import node_facts
from dokimi.results import (
    ASSERTION_COLUMNS,
    METRIC_COLUMNS,
    SCHEMA_VERSION,
    TEST_COLUMNS,
    RunIdentity,
    records_for_assertions,
    records_for_tests,
    write_records,
    write_run_record,
)
from dokimi.validation import (
    ExpectedPair,
    TestError,
    TestOutcome,
    evaluate,
    read_outcome,
    write_validation,
)
from tests.fake_cluster import node

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "worker-01-to-worker-02-udp.yaml"
USAGE = Path(__file__).resolve().parent.parent / "docs" / "usage.md"
DEFINITION = parse_file(EXAMPLE)
IDENTITY = RunIdentity(
    "20261002-120000-a1b2c3", "lab-fra-1", datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
)
FIRST = PlannedPair("pair-001", node_facts(node("worker-01")), node_facts(node("worker-02")))
SECOND = PlannedPair("pair-002", node_facts(node("worker-02")), node_facts(node("worker-03")))
PLAN = Plan(waves=((FIRST,), (SECOND,)))
MEASURED = {
    "dokimi_throughput_bits_per_second": 949_999_999.0,
    "dokimi_packet_loss_percent": 0.39,
    "dokimi_jitter_seconds": 1.1e-05,
    "dokimi_transferred_bytes": 3_562_499_996.0,
    "dokimi_test_duration_seconds": 30.0,
}


def measured(test_id: str = "pair-001") -> Measurement:
    labels = {"test_id": test_id, "test_type": "iperf2", "protocol": "udp", "repetition": "1"}
    return Measurement(test_id, "inbound", dict(MEASURED), labels)


def recorded(tmp_path: Path) -> tuple[list[TestOutcome], dict[str, Measurement]]:
    """What the two tests left: the first measured and failed, the second never measured."""
    measurement = measured()
    write_measurement(measurement, tmp_path)
    pair = ExpectedPair("pair-001", "worker-01", "worker-02")
    write_validation(
        "pair-001",
        [evaluate(c, {"pair-001": measurement}, [pair]) for c in DEFINITION.criteria],
        tmp_path,
    )
    # The second test ended before its criteria, with its category.
    write_validation(
        "pair-002",
        [],
        tmp_path,
        error=TestError(ErrorCategory.IMAGE_PULL, "pair-002 could not pull its image"),
    )
    tests = [read_outcome(tmp_path, "pair-001"), read_outcome(tmp_path, "pair-002")]
    return tests, {"pair-001": measurement}


# -- the test grain -------------------------------------------------------------------------


def test_every_planned_test_has_a_record_whatever_it_came_to(tmp_path: Path) -> None:
    # A file of successes only would be a biased one.
    tests, measurements = recorded(tmp_path)

    records = records_for_tests(IDENTITY, DEFINITION, PLAN, tests, measurements, {"pair-001": True})

    assert [r["test_id"] for r in records] == ["pair-001", "pair-002"]
    failed, errored = records
    assert (failed["outcome"], failed["error_category"]) == ("fail", None)
    assert (errored["outcome"], errored["error_category"]) == ("error", "image_pull")
    assert errored["error_detail"] == "pair-002 could not pull its image"
    assert all(errored[column] is None for column in METRIC_COLUMNS)


def test_a_test_record_carries_the_run_the_test_and_its_parameters(tmp_path: Path) -> None:
    tests, measurements = recorded(tmp_path)

    (record, _) = records_for_tests(
        IDENTITY, DEFINITION, PLAN, tests, measurements, {"pair-001": True}
    )

    assert {key: record[key] for key in TEST_COLUMNS[:12]} == {
        "schema": SCHEMA_VERSION,
        "run_id": "20261002-120000-a1b2c3",
        "cluster": "lab-fra-1",
        "run_started_at": "2026-10-02T12:00:00Z",
        "test_id": "pair-001",
        "repetition": 1,
        "test_type": "iperf2",
        "protocol": "udp",
        "source_node": "worker-01",
        "target_node": "worker-02",
        "duration_seconds": 30,
        "target_bandwidth_bits_per_second": 1_000_000_000,
    }
    assert (record["measurement_source"], record["measurement_direction"]) == ("direct", "inbound")
    assert record["history_captured"] is True


def test_the_metric_values_are_the_ones_the_test_read(tmp_path: Path) -> None:
    # Acceptance criterion: the values in the artifact are the ones the verdict came from,
    # in base units, with no rounding between the two.
    tests, measurements = recorded(tmp_path)

    (record, _) = records_for_tests(IDENTITY, DEFINITION, PLAN, tests, measurements, {})

    for name, value in MEASURED.items():
        assert record[name.removeprefix("dokimi_")] == value
    assert record["history_captured"] is None


def test_a_value_that_is_not_a_finite_number_is_left_out_of_both_files(tmp_path: Path) -> None:
    # An exporter's NaN or Inf parses as a float, but JSON has no such number: a bare NaN
    # would make the line invalid for any strict reader. Validation already made the test
    # an exporter_parse error, which its record says.
    values = {
        **MEASURED,
        "dokimi_jitter_seconds": float("nan"),
        "dokimi_throughput_bits_per_second": float("inf"),
        "dokimi_packet_loss_percent": float("-inf"),
    }
    measurement = Measurement("pair-001", "inbound", values, measured().labels)
    tests = [read_outcome(tmp_path, "pair-001")]
    test_rows = records_for_tests(IDENTITY, DEFINITION, PLAN, tests, {"pair-001": measurement}, {})

    write_records(tmp_path, test_rows, [])

    def strict(constant: str) -> None:
        raise AssertionError(f"{constant} is not JSON")

    line = (tmp_path / "tests.jsonl").read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line, parse_constant=strict)
    assert record["jitter_seconds"] is None
    assert record["throughput_bits_per_second"] is None
    assert record["packet_loss_percent"] is None
    assert record["transferred_bytes"] == MEASURED["dokimi_transferred_bytes"]
    with (tmp_path / "tests.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    first = dict(zip(TEST_COLUMNS, rows[1], strict=True))
    assert first["jitter_seconds"] == first["throughput_bits_per_second"] == ""
    assert first["packet_loss_percent"] == ""


def test_a_repetition_is_a_number_and_none_without_a_measurement(tmp_path: Path) -> None:
    tests, measurements = recorded(tmp_path)

    measured_test, unmeasured = records_for_tests(
        IDENTITY, DEFINITION, PLAN, tests, measurements, {}
    )

    assert measured_test["repetition"] == 1
    assert unmeasured["repetition"] is None


def test_a_planned_test_that_wrote_nothing_is_an_error_with_its_reason(tmp_path: Path) -> None:
    # No data is never a pass, in the files too.
    tests = [read_outcome(tmp_path, "pair-001"), read_outcome(tmp_path, "pair-002")]

    records = records_for_tests(IDENTITY, DEFINITION, PLAN, tests, {}, {})

    assert [r["outcome"] for r in records] == ["error", "error"]
    assert records[0]["error_detail"].startswith("it ended before its outcome was recorded")


# -- the assertion grain --------------------------------------------------------------------


def test_every_assertion_is_recorded_passing_ones_included(tmp_path: Path) -> None:
    # Acceptance criterion: one record per criterion and pair, not only the failures.
    tests, _ = recorded(tmp_path)

    records = records_for_assertions(IDENTITY, tests)

    assert [(r["metric"], r["outcome"]) for r in records] == [
        ("throughput", "fail"),
        ("packetLoss", "fail"),
    ]
    throughput = records[0]
    assert throughput["criterion"] == "Throughput Should Be At Least 950 megabits per second"
    assert (throughput["comparison"], throughput["threshold"], throughput["unit"]) == (
        "atLeast",
        "950",
        "megabits per second",
    )
    assert throughput["promql_equivalent"].endswith("< 950000000")


def test_a_measured_value_is_the_string_the_evaluation_compared(tmp_path: Path) -> None:
    tests, _ = recorded(tmp_path)
    evaluated = json.loads((tmp_path / "pair-001.validation.json").read_text(encoding="utf-8"))

    records = records_for_assertions(IDENTITY, tests)

    assert [r["measured"] for r in records] == [
        criterion["pairs"][0]["value"] for criterion in evaluated["criteria"]
    ]
    assert records[0]["measured"] == "949.999999"


def test_a_test_that_ended_before_its_criteria_has_no_assertions(tmp_path: Path) -> None:
    tests, _ = recorded(tmp_path)

    records = records_for_assertions(IDENTITY, tests)

    assert {r["test_id"] for r in records} == {"pair-001"}


# -- the files ------------------------------------------------------------------------------


def test_both_grains_are_written_as_csv_and_jsonl(tmp_path: Path) -> None:
    # Acceptance criterion: both files, on every run, without being asked for.
    tests, measurements = recorded(tmp_path / "pairs")
    test_rows = records_for_tests(IDENTITY, DEFINITION, PLAN, tests, measurements, {})
    assertion_rows = records_for_assertions(IDENTITY, tests)

    written = write_records(tmp_path, test_rows, assertion_rows)

    assert [path.name for path in written] == [
        "tests.csv",
        "assertions.csv",
        "tests.jsonl",
        "assertions.jsonl",
    ]
    with (tmp_path / "tests.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    assert rows[0] == list(TEST_COLUMNS)
    assert len(rows) == 3
    first = dict(zip(TEST_COLUMNS, rows[1], strict=True))
    assert first["throughput_bits_per_second"] == "949999999.0"
    assert first["jitter_seconds"] == "1.1e-05"
    assert first["history_captured"] == ""
    lines = (tmp_path / "tests.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {column: row[column] for column in TEST_COLUMNS} for row in test_rows
    ]
    with (tmp_path / "assertions.csv").open(encoding="utf-8", newline="") as file:
        assert next(csv.reader(file)) == list(ASSERTION_COLUMNS)


def test_files_with_nothing_in_them_still_have_their_header(tmp_path: Path) -> None:
    write_records(tmp_path, [], [])

    assert (tmp_path / "tests.csv").read_text(encoding="utf-8") == ",".join(TEST_COLUMNS) + "\n"
    assert (tmp_path / "assertions.jsonl").read_text(encoding="utf-8") == ""


def test_the_usage_guide_names_every_column(tmp_path: Path) -> None:
    # The columns are an interface: one appended without a line in the guide would be
    # an undocumented one.
    guide = USAGE.read_text(encoding="utf-8")
    section = guide[guide.index("### The result files") : guide.index("### validation.json")]

    missing = [c for c in (*TEST_COLUMNS, *ASSERTION_COLUMNS) if f"`{c}`" not in section]

    assert missing == []


def test_the_run_record_leads_with_its_schema(tmp_path: Path) -> None:
    path = write_run_record(tmp_path, {"run_id": IDENTITY.run_id, "ended_at": IDENTITY.started_at})

    document = json.loads(path.read_text(encoding="utf-8"))
    assert list(document) == ["schema", "run_id", "ended_at"]
    assert document["ended_at"] == "2026-10-02T12:00:00Z"
