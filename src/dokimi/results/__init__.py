"""Phase 9b: the run's result files, CSV and JSONL, written on every run.

A run's primary output, not an extra: whatever else a reader has, these say what was
measured and what it came to, in files that outlive the cluster. They are written from
what the run itself recorded, the measurement each test read directly and the outcome its
criteria came to, never re-derived and never read back from Prometheus. So a value here is
the value the verdict was computed from.

**Two grains, each as CSV and as JSONL, the same records serialized twice:**

* ``tests.csv`` / ``tests.jsonl``: one record per planned test, with the run's and the
  test's identity, the parameters that shape the measurement, every result metric in its
  base unit, and how the test ended: ``pass``, ``fail`` or ``error`` with its category. A
  test that failed or errored has its record too, since a file of successes only is a
  biased one.
* ``assertions.csv`` / ``assertions.jsonl``: one record per criterion and pair, passing or
  not: the criterion as written and as fields, the measured value in the criterion's own
  unit, exactly as its evaluation had it, and the outcome.

**And one per run,** ``run.json``, written at the very end, once the run's verdict, what
it kept and how its cleanup went are known: the effective configuration, the images by
digest, the plan, the monitoring it used, its scrape targets, and the run's outcome.

The columns are versioned by ``schema``: a column may be added at the end, and removing,
renaming or reordering one raises the version.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from dokimi.k8s.ownership import format_timestamp
from dokimi.measurement import RESULTS, Measurement
from dokimi.model import TestDefinition
from dokimi.planning import Plan
from dokimi.validation import TestOutcome

__all__ = [
    "ASSERTIONS_CSV",
    "ASSERTIONS_JSONL",
    "ASSERTION_COLUMNS",
    "METRIC_COLUMNS",
    "RUN_RECORD",
    "SCHEMA_VERSION",
    "TESTS_CSV",
    "TESTS_JSONL",
    "TEST_COLUMNS",
    "RunIdentity",
    "records_for_assertions",
    "records_for_tests",
    "write_records",
    "write_run_record",
]

SCHEMA_VERSION = 1

TESTS_CSV = "tests.csv"
TESTS_JSONL = "tests.jsonl"
ASSERTIONS_CSV = "assertions.csv"
ASSERTIONS_JSONL = "assertions.jsonl"
RUN_RECORD = "run.json"

#: Every result metric a test type can produce, by its series name without ``dokimi_``,
#: in base units. Fixed, so that the columns don't depend on what one run measured.
METRIC_COLUMNS: tuple[str, ...] = tuple(name.removeprefix("dokimi_") for name in sorted(RESULTS))

TEST_COLUMNS: tuple[str, ...] = (
    "schema",
    "run_id",
    "cluster",
    "run_started_at",
    "test_id",
    "repetition",
    "test_type",
    "protocol",
    "source_node",
    "target_node",
    "duration_seconds",
    "target_bandwidth_bits_per_second",
    "outcome",
    "error_category",
    "error_detail",
    "measurement_source",
    "measurement_direction",
    "history_captured",
    *METRIC_COLUMNS,
)

ASSERTION_COLUMNS: tuple[str, ...] = (
    "schema",
    "run_id",
    "cluster",
    "test_id",
    "source_node",
    "target_node",
    "criterion",
    "metric",
    "statistic",
    "comparison",
    "threshold",
    "unit",
    "measured",
    "outcome",
    "error_category",
    "error_detail",
    "promql_equivalent",
)


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """What every record of a run carries to say which run it is."""

    run_id: str
    cluster: str
    started_at: datetime


def records_for_tests(
    identity: RunIdentity,
    definition: TestDefinition,
    plan: Plan,
    tests: Sequence[TestOutcome],
    measurements: Mapping[str, Measurement],
    history: Mapping[str, bool],
) -> list[dict[str, Any]]:
    """One record per planned test, in the plan's order, whether it passed or not.

    *measurements* and *history* are by test id: what each test read directly, and whether
    Prometheus came to hold it. A test without a measurement has empty metric columns,
    and its outcome says why.

    A record carries the run's start but not its end. These records are written before
    the run ends, in phase 9b, and the run's end, its exit code and its cleanup are only
    known in phase 11. So they are in ``run.json``, which is written then, and not here.
    """
    outcomes = {test.test_id: test for test in tests}
    records = []
    for pair in plan.pairs:
        test = outcomes.get(pair.test_id)
        measurement = measurements.get(pair.test_id)
        record: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "run_id": identity.run_id,
            "cluster": identity.cluster,
            "run_started_at": format_timestamp(identity.started_at),
            "test_id": pair.test_id,
            "repetition": None if measurement is None else _repetition(measurement.labels),
            "test_type": definition.test_type.value,
            "protocol": definition.protocol.value,
            "source_node": pair.source.name,
            "target_node": pair.target.name,
            "duration_seconds": _number(definition.duration.seconds),
            "target_bandwidth_bits_per_second": _number(
                definition.target_bandwidth.bits_per_second
            ),
            # A planned test that recorded nothing is an error: no data is never a pass.
            "outcome": "error" if test is None else test.outcome.value,
            "error_category": _category(test),
            "error_detail": _detail(test),
            "measurement_source": None if measurement is None else measurement.source,
            "measurement_direction": None if measurement is None else measurement.direction,
            "history_captured": history.get(pair.test_id),
        }
        for column in METRIC_COLUMNS:
            value = None if measurement is None else measurement.values.get(f"dokimi_{column}")
            record[column] = _finite(value)
        records.append(record)
    return records


def records_for_assertions(
    identity: RunIdentity, tests: Sequence[TestOutcome]
) -> list[dict[str, Any]]:
    """One record per criterion and pair, from what each test's evaluation recorded.

    The measured value is the decimal string the evaluation compared, in the criterion's
    unit: the same characters, so nothing between the verdict and this file rounds it.
    A test that ended before its criteria has no assertions; its test record says why.
    """
    records = []
    for test in tests:
        for criterion in (test.document or {}).get("criteria", []):
            for pair in criterion.get("pairs", []):
                records.append(
                    {
                        "schema": SCHEMA_VERSION,
                        "run_id": identity.run_id,
                        "cluster": identity.cluster,
                        "test_id": pair.get("test_id", test.test_id),
                        "source_node": pair.get("source_node"),
                        "target_node": pair.get("target_node"),
                        "criterion": criterion.get("phrase"),
                        "metric": criterion.get("metric"),
                        "statistic": criterion.get("statistic"),
                        "comparison": criterion.get("comparison"),
                        "threshold": criterion.get("threshold"),
                        "unit": criterion.get("unit"),
                        "measured": pair.get("value"),
                        "outcome": pair.get("outcome"),
                        "error_category": pair.get("category"),
                        "error_detail": pair.get("detail") or None,
                        "promql_equivalent": criterion.get("promql_equivalent"),
                    }
                )
    return records


def write_records(
    directory: Path,
    tests: Sequence[Mapping[str, Any]],
    assertions: Sequence[Mapping[str, Any]],
) -> list[Path]:
    """Write both grains as CSV and as JSONL into *directory*. Returns the four paths.

    The files are written even when empty, with their header: a run with nothing to
    report still says so in the shape a reader expects.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, columns, records in (
        (TESTS_CSV, TEST_COLUMNS, tests),
        (ASSERTIONS_CSV, ASSERTION_COLUMNS, assertions),
    ):
        path = directory / name
        path.write_text(_csv(columns, records), encoding="utf-8", newline="")
        written.append(path)
    for name, columns, records in (
        (TESTS_JSONL, TEST_COLUMNS, tests),
        (ASSERTIONS_JSONL, ASSERTION_COLUMNS, assertions),
    ):
        path = directory / name
        path.write_text(_jsonl(columns, records), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def write_run_record(directory: Path, record: Mapping[str, Any]) -> Path:
    """Write ``run.json`` into *directory*: the run's own record, with ``schema`` first."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / RUN_RECORD
    document = {"schema": SCHEMA_VERSION, **record}
    path.write_text(
        json.dumps(document, indent=2, default=_json_default, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _category(test: TestOutcome | None) -> str | None:
    """Why a test is an error, when one category says it: its own, or its criteria's."""
    if test is None:
        return "metrics_missing"
    error = test.error
    if error is not None:
        return None if error.category is None else error.category.value
    categories: list[str] = []
    for criterion in (test.document or {}).get("criteria", []):
        categories.extend(c for c in criterion.get("categories") or [] if c not in categories)
    return ",".join(categories) or None


def _detail(test: TestOutcome | None) -> str | None:
    if test is None:
        return None
    error = test.error
    if error is not None:
        return error.detail or None
    if test.document is None:
        return test.problem or None
    if not test.document.get("criteria"):
        return "nothing asserted: the test has no pass criterion"
    return None


def _repetition(labels: Mapping[str, str]) -> int | None:
    """The ``repetition`` label as the number it counts, or none where it isn't one."""
    value = labels.get("repetition", "")
    return int(value) if value.isdecimal() else None


def _finite(value: float | None) -> float | None:
    """A metric value, or none where it isn't a finite number.

    An exporter's ``NaN`` or ``+Inf`` parses as a float, and validation makes its test an
    ``exporter_parse`` error. JSON has no such number, so the value is left out, ``null``
    and an empty cell, and the record's outcome and category say why.
    """
    return None if value is None or not math.isfinite(value) else value


def _number(value: Decimal) -> int | float:
    """A definition's exact number, as JSON and CSV write it: an int where it is whole."""
    return int(value) if value == value.to_integral_value() else float(value)


def _cell(value: Any) -> str:
    """A CSV cell: empty for none, ``true``/``false``, and a float's shortest exact form."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _csv(columns: Sequence[str], records: Iterable[Mapping[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for record in records:
        writer.writerow([_cell(record.get(column)) for column in columns])
    return buffer.getvalue()


def _jsonl(columns: Sequence[str], records: Iterable[Mapping[str, Any]]) -> str:
    return "".join(
        json.dumps(
            {column: record.get(column) for column in columns},
            default=_json_default,
            allow_nan=False,
        )
        + "\n"
        for record in records
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not a result value")
