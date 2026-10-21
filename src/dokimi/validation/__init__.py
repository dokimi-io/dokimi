"""Phase 8: pass criteria evaluated over the result vector.

A criterion is a canonical record (:class:`~dokimi.model.Criterion`), and it is evaluated
against what phase 7 read directly from the exporters. This layer classifies, and a Robot
status is a rendering of its classification, never its source.

**Universally quantified over the expected pairs.** A criterion passes only if it holds
for every pair the plan expects, and the expected set comes from the plan, never from what
came back. So an empty or partial result can't be mistaken for a pass.

**Three outcomes:**

* ``pass``: measured, and the criterion holds;
* ``fail``: measured, and it doesn't, a finding about the network;
* ``error``: not validly measured, never a pass and never a ``fail``. A pair without the
  series is ``metrics_missing``; a value that isn't a finite number is
  ``exporter_parse``. A metric the test type never produces is reported once, naming it,
  rather than once per pair.

A criterion with any ``error`` pair is ``error``; otherwise any ``fail`` makes it
``fail``. The pairs that were measured are still evaluated and reported.

**Units are converted on read.** The measured value, in the metric's base unit, is
converted into the unit the criterion was written in, and compared there, as a
:class:`~decimal.Decimal`, with the threshold the user wrote. Every value in a message is
in that unit too.

**The equivalent PromQL** is recorded with each evaluation, labelled as an equivalent:
the verdict came from the direct read, and the expression is how to reproduce it from
Prometheus's history by hand.
"""

from __future__ import annotations

import enum
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from dokimi.measurement import RESULTS, Measurement
from dokimi.model import Comparison, Criterion, Metric

__all__ = [
    "MAX_LISTED",
    "METRIC_SERIES",
    "VALIDATION_SUFFIX",
    "Evaluation",
    "ExpectedPair",
    "Outcome",
    "PairResult",
    "evaluate",
    "format_value",
    "outcome_of",
    "phrase",
    "promql_equivalent",
    "read_validation",
    "write_validation",
]

#: Each criterion metric's series, as the exporters serve it. ``latency``
#: has none yet: no test type in Milestone-01 measures it.
METRIC_SERIES: Mapping[Metric, str] = {
    Metric.THROUGHPUT: "dokimi_throughput_bits_per_second",
    Metric.PACKET_LOSS: "dokimi_packet_loss_percent",
    Metric.JITTER: "dokimi_jitter_seconds",
}
#: How many pairs a message lists for each of its failures and errors, worst first.
MAX_LISTED = 10
#: ``<test-id>.validation.json``, next to the pair's ``output.xml``.
VALIDATION_SUFFIX = ".validation.json"
SCHEMA_VERSION = 1

METRICS_MISSING = "metrics_missing"
EXPORTER_PARSE = "exporter_parse"


class Outcome(enum.Enum):
    """The outcomes phase 8 can reach, in order of precedence."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return _RANK[self]


_RANK = {Outcome.PASS: 0, Outcome.FAIL: 1, Outcome.ERROR: 2}


@dataclass(frozen=True, slots=True)
class ExpectedPair:
    """A pair the plan expects a result from: the set a criterion is quantified over."""

    test_id: str
    source_node: str
    target_node: str

    def __str__(self) -> str:
        return f"{self.source_node} -> {self.target_node} ({self.test_id})"


@dataclass(frozen=True, slots=True)
class PairResult:
    """One pair's outcome for one criterion."""

    pair: ExpectedPair
    outcome: Outcome
    #: The measured value in the criterion's unit; ``None`` for an error.
    value: Decimal | None = None
    #: The error category, for an error: ``metrics_missing`` or ``exporter_parse``.
    category: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Evaluation:
    """A criterion evaluated over every expected pair."""

    criterion: Criterion
    #: The criterion as the user wrote it.
    phrase: str
    outcome: Outcome
    pairs: tuple[PairResult, ...]
    #: Set when the test type never produces the metric: the one error, for every pair.
    not_produced: str | None
    #: How to reproduce the verdict from Prometheus: an equivalent, not what ran.
    promql_equivalent: str

    @property
    def failed(self) -> tuple[PairResult, ...]:
        """The pairs that violated the criterion, worst first."""
        failed = [result for result in self.pairs if result.outcome is Outcome.FAIL]
        return tuple(sorted(failed, key=self._badness, reverse=True))

    @property
    def errored(self) -> tuple[PairResult, ...]:
        return tuple(result for result in self.pairs if result.outcome is Outcome.ERROR)

    @property
    def categories(self) -> tuple[str, ...]:
        """The error categories, each once, in order of first appearance."""
        return tuple(dict.fromkeys(r.category for r in self.errored if r.category))

    def message(self) -> str:
        """What a person reads.

        The criterion as written, a count before any detail, the worst pairs first with
        their values in the user's unit, truncated, and errors apart from failures.
        """
        if self.outcome is Outcome.PASS:
            return (
                f"{self.phrase}: holds for all {len(self.pairs)} pair(s), the closest being "
                + self._closest()
            )
        lines = []
        if self.errored:
            categories = ", ".join(self.categories)
            lines.append(
                f"ERROR ({categories}): {self.phrase}: {len(self.errored)} of "
                f"{len(self.pairs)} pair(s) could not be evaluated. This is not a finding "
                "about the network"
            )
            if self.not_produced is not None:
                lines.append(f"  {self.not_produced}")
            else:
                lines.extend(
                    f"  {result.pair}: {result.category}: {result.detail}"
                    for result in self.errored[:MAX_LISTED]
                )
                lines.extend(self._more(len(self.errored)))
        if self.failed:
            lines.append(
                f"{self.phrase}: {len(self.failed)} of {len(self.pairs)} pair(s) violated "
                "this criterion"
            )
            lines.extend(
                f"  {result.pair}: {self._value(result)}, {self._needs()}"
                for result in self.failed[:MAX_LISTED]
            )
            lines.extend(self._more(len(self.failed)))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        criterion = self.criterion
        return {
            "phrase": self.phrase,
            "metric": criterion.metric.value,
            "statistic": criterion.statistic.value if criterion.statistic else None,
            "comparison": criterion.comparison.value,
            "threshold": str(criterion.threshold),
            "unit": criterion.unit.value,
            "outcome": self.outcome.value,
            "categories": list(self.categories),
            "promql_equivalent": self.promql_equivalent,
            "pairs": [
                {
                    "test_id": result.pair.test_id,
                    "source_node": result.pair.source_node,
                    "target_node": result.pair.target_node,
                    "outcome": result.outcome.value,
                    "value": None if result.value is None else str(result.value),
                    "category": result.category,
                    "detail": result.detail,
                }
                for result in self.pairs
            ],
        }

    def _badness(self, result: PairResult) -> Decimal:
        """How far a measured pair is from the threshold, on the wrong side counting most."""
        assert result.value is not None
        threshold = self.criterion.threshold
        comparison = self.criterion.comparison
        if comparison is Comparison.AT_LEAST:
            return threshold - result.value
        if comparison is Comparison.AT_MOST:
            return result.value - threshold
        return abs(result.value - threshold)

    def _closest(self) -> str:
        measured = [result for result in self.pairs if result.value is not None]
        if not measured:
            return "none"
        closest = max(measured, key=self._badness)
        return f"{closest.pair} at {self._value(closest)}"

    def _value(self, result: PairResult) -> str:
        assert result.value is not None
        return f"{format_value(result.value)} {self.criterion.unit.value}"

    def _needs(self) -> str:
        bound = {
            Comparison.AT_LEAST: "at least",
            Comparison.AT_MOST: "at most",
            Comparison.EXACTLY: "exactly",
        }[self.criterion.comparison]
        return f"needs {bound} {self.criterion.threshold}"

    def _more(self, count: int) -> list[str]:
        if count <= MAX_LISTED:
            return []
        return [f"  ... and {count - MAX_LISTED} more, in each test's {VALIDATION_SUFFIX[1:]}"]


def phrase(criterion: Criterion) -> str:
    """The criterion as a Robot suite writes it: ``Throughput Should Be At Least 950 …``."""
    words = [
        *([criterion.statistic.robot_phrasing] if criterion.statistic else []),
        criterion.metric.robot_phrasing,
        "Should Be",
        criterion.comparison.robot_phrasing,
        str(criterion.threshold),
        criterion.unit.value,
    ]
    return " ".join(words)


def evaluate(
    criterion: Criterion,
    measurements: Mapping[str, Measurement],
    expected: Sequence[ExpectedPair],
    *,
    written: str | None = None,
    run_id: str = "",
) -> Evaluation:
    """Evaluate *criterion* for every pair in *expected*, from *measurements* by test id.

    *written* is the criterion as the suite has it, for the message; :func:`phrase`
    renders it otherwise. A pair with no measurement at all is ``metrics_missing``, like
    one whose measurement lacks the series.
    """
    if not expected:
        # Nothing to quantify over is not a vacuous pass: the plan always has a pair, so
        # this is a caller's bug, and passing would hide it.
        raise ValueError("a criterion is evaluated over at least one expected pair")
    series = METRIC_SERIES.get(criterion.metric)
    not_produced = _not_produced(criterion, series, measurements, expected)
    results = []
    for pair in expected:
        if not_produced is not None:
            results.append(PairResult(pair, Outcome.ERROR, category=METRICS_MISSING))
            continue
        results.append(_evaluate_pair(criterion, series, measurements.get(pair.test_id), pair))
    outcome = max((result.outcome for result in results), key=lambda o: o.rank)
    return Evaluation(
        criterion=criterion,
        phrase=written or phrase(criterion),
        outcome=outcome,
        pairs=tuple(results),
        not_produced=not_produced,
        promql_equivalent=promql_equivalent(criterion, run_id),
    )


def _evaluate_pair(
    criterion: Criterion, series: str | None, measurement: Measurement | None, pair: ExpectedPair
) -> PairResult:
    if measurement is None:
        return PairResult(
            pair,
            Outcome.ERROR,
            category=METRICS_MISSING,
            detail="no measurement: its test ended before its result was read",
        )
    if series is None or series not in measurement.values:
        return PairResult(
            pair,
            Outcome.ERROR,
            category=METRICS_MISSING,
            detail=f"its result has no {series or criterion.metric.value}",
        )
    raw = measurement.values[series]
    if not math.isfinite(raw):
        return PairResult(
            pair,
            Outcome.ERROR,
            category=EXPORTER_PARSE,
            detail=f"{series} is {raw!r}, not a number a criterion can be checked against",
        )
    # repr() is the shortest string that round-trips the float, so the Decimal holds
    # exactly what was read, without binary noise in the digits a message shows.
    value = Decimal(repr(raw)) / criterion.unit.base_factor
    holds = _holds(criterion.comparison, value, criterion.threshold)
    return PairResult(pair, Outcome.PASS if holds else Outcome.FAIL, value=value)


def _holds(comparison: Comparison, value: Decimal, threshold: Decimal) -> bool:
    if comparison is Comparison.AT_LEAST:
        return value >= threshold
    if comparison is Comparison.AT_MOST:
        return value <= threshold
    return value == threshold


def _not_produced(
    criterion: Criterion,
    series: str | None,
    measurements: Mapping[str, Measurement],
    expected: Sequence[ExpectedPair],
) -> str | None:
    """Why no pair can have this metric, if the test type never produces it.

    Reported once, naming the metric and the test type, instead of as one identical
    error per pair. Decided from what the exporters say they ran, so only where every
    expected pair has a measurement that lacks the series. A metric no exporter serves at
    all is never produced; for one that some protocol produces, every measurement has to
    name a protocol that doesn't. Anything less certain is a missing series per pair.
    """
    found = [measurements.get(pair.test_id) for pair in expected]
    if any(m is None for m in found):
        return None
    present = [m for m in found if m is not None]
    if series is not None:
        if any(series in m.values for m in present):
            return None
        produced = RESULTS.get(series, frozenset())
        if any(m.labels.get("protocol", "") in produced | {""} for m in present):
            return None
    kinds = sorted({_kind(m) for m in present})
    return (
        f"{', '.join(kinds)} does not produce {criterion.metric.value}, so no pair can be "
        "checked against it"
    )


def _kind(measurement: Measurement) -> str:
    """``iperf2 over tcp``: what ran, as its labels say."""
    test_type = measurement.labels.get("test_type") or "this test type"
    protocol = measurement.labels.get("protocol")
    return f"{test_type} over {protocol}" if protocol else test_type


def promql_equivalent(criterion: Criterion, run_id: str = "") -> str:
    """The PromQL that returns the pairs violating *criterion*: empty means none did.

    An equivalent of what phase 8 evaluated from the direct read, for reproducing a
    verdict from Prometheus by hand, and labelled as one wherever it is recorded. Unlike
    the evaluation, an empty answer here can also mean the series never arrived, which is
    why it is not what decides.
    """
    series = METRIC_SERIES.get(criterion.metric, f"<no series for {criterion.metric.value}>")
    selector = ",".join([*([f'run_id="{run_id}"'] if run_id else []), 'direction="inbound"'])
    threshold = criterion.threshold * criterion.unit.base_factor
    violation = {
        Comparison.AT_LEAST: "<",
        Comparison.AT_MOST: ">",
        Comparison.EXACTLY: "!=",
    }[criterion.comparison]
    return f"{series}{{{selector}}} {violation} {format_value(threshold)}"


def format_value(value: Decimal) -> str:
    """A number for a person: up to seven significant digits, never an exponent.

    ``1e-05`` has no place in a message a person reads. Values here are already in the
    user's unit, so they are rarely tiny, but never written as one.
    """
    if value == 0:
        return "0"
    rounded = Decimal(f"{value:.7g}")
    text = format(rounded, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# -- one pair's outcome, and where it is kept -----------------------------------------------


def outcome_of(evaluations: Sequence[Evaluation]) -> Outcome:
    """A test's outcome from its criteria: the worst of them, ``pass`` if there are none.

    A test with no criteria was measured and asserted nothing, so it passes, as Robot's
    status already says. What made it unmeasurable stops it earlier, in phase 6 or 7.
    """
    return max((e.outcome for e in evaluations), key=lambda o: o.rank, default=Outcome.PASS)


def write_validation(test_id: str, evaluations: Sequence[Evaluation], directory: Path) -> Path:
    """Write ``<test-id>.validation.json`` into *directory*, next to the pair's output.

    What M1-14's result artifact and M1-16's outcome read: the classification itself,
    never Robot's rendering of it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{test_id}{VALIDATION_SUFFIX}"
    document = {
        "schema": SCHEMA_VERSION,
        "test_id": test_id,
        "outcome": outcome_of(evaluations).value,
        "criteria": [evaluation.to_dict() for evaluation in evaluations],
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def read_validation(directory: Path, test_id: str) -> dict[str, Any] | None:
    """What a pair's Robot process wrote about its criteria, or ``None`` if nothing."""
    path = directory / f"{test_id}{VALIDATION_SUFFIX}"
    if not path.is_file():
        return None
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document
