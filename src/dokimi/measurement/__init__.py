"""Phase 7: the measurement, read directly from the exporters (M1-12).

**The direct read is authoritative.** Once a test has completed, each of its pods'
exporters is read over ``/metrics``, the same surface Prometheus scrapes: one format, one
code path, so a disagreement between the two readers says something about delivery, not
about two endpoints. The verdict is evaluated against what this read returns, in phase 8.

**The receiver's view decides.** Each pod exports its own view, labelled ``direction``.
The ``inbound`` one, the server's, is what arrived: its throughput, and for
UDP the packet loss and jitter only the receiver can see. The sender's view is read too,
and must be complete, but decides nothing.

**Absent data is an error, never a pass**:

* :class:`MetricsMissingError` (``metrics_missing``): an exporter that can't be reached,
  serves no series for the test, or doesn't hold a completed result;
* :class:`ExporterParseError` (``exporter_parse``): a line that isn't exposition format,
  a value that isn't a number, or a ``dokimi_`` series without a required label, which is
  an exporter defect.

**Then Prometheus is asked** (:func:`confirm_scrape`), before teardown, whether the same
series reached it, for at most :func:`scrape_confirmation_timeout`. One that didn't is a
warning naming the test: its history was not captured, and the verdict stands. One that
did is compared with the direct read, and a difference beyond :data:`TOLERANCE` is a
diagnostic of its own rather than one path silently winning.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dokimi.error_categories import ErrorCategory

__all__ = [
    "AUTHORITATIVE_DIRECTION",
    "REQUIRED_LABELS",
    "TOLERANCE",
    "Disagreement",
    "ExporterParseError",
    "Measurement",
    "MeasurementError",
    "MetricsMissingError",
    "Sample",
    "ScrapeReport",
    "confirm_scrape",
    "fetch_metrics",
    "measure",
    "parse_exposition",
    "read_measurement",
    "scrape_confirmation_timeout",
    "write_measurement",
]

#: The label set every result metric must carry. ``direction`` is required on top.
REQUIRED_LABELS = (
    "cluster",
    "run_id",
    "test_id",
    "repetition",
    "test_type",
    "source_node",
    "target_node",
    "source_pod",
    "target_pod",
    "source_ip",
    "target_ip",
    "protocol",
)
#: The view the verdict is read from: what the receiver got.
AUTHORITATIVE_DIRECTION = "inbound"
#: How far the direct read and Prometheus may differ before it is a diagnostic. Both read
#: the gauge of a completed result from the same surface, so anything beyond a float's
#: rounding is a bug or a race, and should show (decided 2026-10-18).
TOLERANCE = 1e-9

COMPLETED = "dokimi_test_completed"
#: The results, by metric name, and the protocols that have them.
RESULTS: Mapping[str, frozenset[str]] = {
    "dokimi_throughput_bits_per_second": frozenset({"tcp", "udp"}),
    "dokimi_transferred_bytes": frozenset({"tcp", "udp"}),
    "dokimi_test_duration_seconds": frozenset({"tcp", "udp"}),
    "dokimi_packet_loss_percent": frozenset({"udp"}),
    "dokimi_jitter_seconds": frozenset({"udp"}),
}
MEASUREMENT_SUFFIX = ".measurement.json"

_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)(?:\s+\S+)?$"
)
_LABEL = re.compile(r'\s*(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"\s*(?:,|$)')
_ESCAPES = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}


class MeasurementError(RuntimeError):
    """The test ran, and its result could not be validly read: an error, not a finding."""

    #: What a subclass says went wrong. The base class is never raised itself.
    category: ErrorCategory

    def __init__(self, test_id: str, detail: str) -> None:
        self.test_id = test_id
        self.detail = detail
        super().__init__(f"{test_id}: {self.category}: {detail}")


class MetricsMissingError(MeasurementError):
    """An exporter served no completed result for the test."""

    category = ErrorCategory.METRICS_MISSING


class ExporterParseError(MeasurementError):
    """An exporter served something that isn't a result dokimi can read."""

    category = ErrorCategory.EXPORTER_PARSE


@dataclass(frozen=True, slots=True)
class Sample:
    """One series and its value, as one exposition line has it."""

    name: str
    labels: Mapping[str, str]
    value: float


@dataclass(frozen=True, slots=True)
class Measurement:
    """One test's result, as read directly from its exporters."""

    test_id: str
    #: The view the values come from: :data:`AUTHORITATIVE_DIRECTION`.
    direction: str
    #: Each result metric's value, in its base unit.
    values: Mapping[str, float]
    #: The authoritative series' labels: what identifies it in Prometheus too.
    labels: Mapping[str, str]
    #: Which path the values came from: always the direct read.
    source: str = "direct"

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "test_id": self.test_id,
                    "direction": self.direction,
                    "source": self.source,
                    "values": dict(self.values),
                    "labels": dict(self.labels),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    @classmethod
    def from_json(cls, text: str) -> Measurement:
        document = json.loads(text)
        return cls(
            test_id=document["test_id"],
            direction=document["direction"],
            values={key: float(value) for key, value in document["values"].items()},
            labels=dict(document["labels"]),
            source=document.get("source", "direct"),
        )


# -- the direct read ----------------------------------------------------------------------


def fetch_metrics(url: str, *, timeout: float = 5.0) -> str:
    """``GET`` *url* and return its body: the plain scrape Prometheus makes too."""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - pod URL
        body: bytes = response.read()
    return body.decode("utf-8")


def parse_exposition(text: str, *, test_id: str = "") -> list[Sample]:
    """Every sample in Prometheus's text format. Raises :class:`ExporterParseError`."""
    samples = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if match is None:
            raise ExporterParseError(test_id, f"line {number} is not a sample: {line!r}")
        try:
            value = float(match["value"])
        except ValueError:
            raise ExporterParseError(
                test_id, f"line {number}: {match['value']!r} is not a number"
            ) from None
        samples.append(
            Sample(match["name"], _labels(match["labels"] or "", number, test_id), value)
        )
    return samples


def _labels(text: str, number: int, test_id: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    position = 0
    while position < len(text.rstrip()):
        match = _LABEL.match(text, position)
        if match is None:
            raise ExporterParseError(test_id, f"line {number}: unreadable labels {{{text}}}")
        labels[match["key"]] = re.sub(r'\\[\\"n]', lambda m: _ESCAPES[m[0]], match["value"])
        position = match.end()
    return labels


def measure(
    exporters: Sequence[str],
    *,
    run_id: str,
    test_id: str,
    protocol: str,
    read: Callable[[str], str] = fetch_metrics,
    timeout: float = 30.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Measurement:
    """Read every exporter of a completed test, and return its authoritative result.

    The receiver marks its result complete once the measured connection's line is fully
    written, which can trail the sender's exit by a moment. So a result not yet complete
    is read again, for at most *timeout*; anything unreadable fails at once.

    Raises:
        MetricsMissingError: an exporter unreachable, or no completed result in time.
        ExporterParseError: an exporter serving something that isn't a readable result.
    """
    deadline = clock() + timeout
    while True:
        samples = _read_all(exporters, test_id, read)
        own = [
            s
            for s in samples
            if s.labels.get("run_id") == run_id and s.labels.get("test_id") == test_id
        ]
        _check_labels(own, test_id)
        incomplete = _incomplete(own)
        if not incomplete:
            return _result(own, test_id, protocol)
        if clock() >= deadline:
            raise MetricsMissingError(
                test_id,
                f"no completed result from the {' and '.join(incomplete)} view after "
                f"{timeout:g}s of reading {', '.join(exporters)}",
            )
        sleep(1.0)


def _read_all(exporters: Sequence[str], test_id: str, read: Callable[[str], str]) -> list[Sample]:
    samples: list[Sample] = []
    for url in exporters:
        try:
            text = read(url)
        except OSError as error:
            raise MetricsMissingError(test_id, f"could not read {url}: {error}") from error
        samples.extend(parse_exposition(text, test_id=test_id))
    return samples


def _check_labels(samples: Iterable[Sample], test_id: str) -> None:
    for sample in samples:
        missing = [
            label for label in (*REQUIRED_LABELS, "direction") if not sample.labels.get(label)
        ]
        if missing:
            raise ExporterParseError(
                test_id,
                f"{sample.name} lacks the required label(s) {', '.join(missing)}, which "
                "makes it unmatchable to its test",
            )


def _incomplete(samples: Sequence[Sample]) -> list[str]:
    """The directions, inbound and outbound, without a completed result."""
    completed = {
        sample.labels["direction"]
        for sample in samples
        if sample.name == COMPLETED and sample.value == 1
    }
    return [d for d in (AUTHORITATIVE_DIRECTION, "outbound") if d not in completed]


def _result(samples: Sequence[Sample], test_id: str, protocol: str) -> Measurement:
    view = [s for s in samples if s.labels["direction"] == AUTHORITATIVE_DIRECTION]
    values: dict[str, float] = {}
    for name, protocols in RESULTS.items():
        found = [s for s in view if s.name == name]
        if not found and protocol in protocols:
            raise MetricsMissingError(
                test_id, f"the {AUTHORITATIVE_DIRECTION} view is complete but serves no {name}"
            )
        if len(found) > 1:
            raise ExporterParseError(test_id, f"{name} is served {len(found)} times for one test")
        if found:
            values[name] = found[0].value
    completed = next(s for s in view if s.name == COMPLETED)
    return Measurement(test_id, AUTHORITATIVE_DIRECTION, values, dict(completed.labels))


def write_measurement(measurement: Measurement, directory: Path) -> Path:
    """Write ``<test_id>.measurement.json`` into *directory*, next to the pair's output."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{measurement.test_id}{MEASUREMENT_SUFFIX}"
    path.write_text(measurement.to_json(), encoding="utf-8")
    return path


def read_measurement(directory: Path, test_id: str) -> Measurement | None:
    """The measurement a pair's Robot process wrote, or ``None`` if it wrote none."""
    path = directory / f"{test_id}{MEASUREMENT_SUFFIX}"
    if not path.is_file():
        return None
    return Measurement.from_json(path.read_text(encoding="utf-8"))


# -- the scrape confirmation --------------------------------------------------------------


def scrape_confirmation_timeout(scrape_interval: float) -> float:
    """Three scrape intervals, and at least a minute."""
    return max(3 * scrape_interval, 60.0)


@dataclass(frozen=True, slots=True)
class Disagreement:
    """A value Prometheus holds that differs from the direct read beyond :data:`TOLERANCE`."""

    test_id: str
    metric: str
    direct: float
    prometheus: float


@dataclass(frozen=True, slots=True)
class ScrapeReport:
    """What Prometheus held of each test's result, by the end of the confirmation."""

    scrape_interval: float
    timeout: float
    #: Each confirmed test's values as Prometheus has them.
    confirmed: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    #: The tests whose series never arrived: their history was not captured.
    missing: tuple[str, ...] = ()
    disagreements: tuple[Disagreement, ...] = ()
    #: Why Prometheus could not be asked at all, if it couldn't.
    error: str = ""

    def to_json(self, measurements: Sequence[Measurement]) -> str:
        """``measurement.json``: every value, where it came from, and what Prometheus held."""
        document: dict[str, Any] = {
            "scrape_interval_seconds": self.scrape_interval,
            "confirmation_timeout_seconds": self.timeout,
            "tolerance": TOLERANCE,
            "error": self.error or None,
            "tests": [
                {
                    "test_id": m.test_id,
                    "direction": m.direction,
                    "source": m.source,
                    "values": dict(m.values),
                    "prometheus": dict(self.confirmed.get(m.test_id, {})) or None,
                    "history_captured": m.test_id in self.confirmed,
                    "disagreements": [
                        {"metric": d.metric, "direct": d.direct, "prometheus": d.prometheus}
                        for d in self.disagreements
                        if d.test_id == m.test_id
                    ],
                }
                for m in measurements
            ],
        }
        return json.dumps(document, indent=2) + "\n"


def confirm_scrape(
    query: Callable[[str], list[dict[str, Any]]],
    measurements: Sequence[Measurement],
    *,
    scrape_interval: float,
    timeout: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ScrapeReport:
    """Ask Prometheus, until each test's series has arrived or *timeout* has passed.

    Never raises: a Prometheus that can't be asked confirms nothing, and says why. The
    verdict comes from the direct read and is never retracted here.
    """
    limit = scrape_confirmation_timeout(scrape_interval) if timeout is None else timeout
    deadline = clock() + limit
    confirmed: dict[str, dict[str, float]] = {}
    error = ""
    pending = [m for m in measurements]
    while pending:
        for measurement in list(pending):
            try:
                found = _held(query, measurement)
            except Exception as failure:  # never the verdict's problem
                error = f"{type(failure).__name__}: {failure}"
                continue
            if found and all(name in found for name in measurement.values):
                confirmed[measurement.test_id] = found
                pending.remove(measurement)
        if not pending or clock() >= deadline:
            break
        sleep(scrape_interval)
    disagreements = tuple(
        Disagreement(m.test_id, name, direct, confirmed[m.test_id][name])
        for m in measurements
        if m.test_id in confirmed
        for name, direct in m.values.items()
        if not _agree(direct, confirmed[m.test_id][name])
    )
    return ScrapeReport(
        scrape_interval=scrape_interval,
        timeout=limit,
        confirmed=confirmed,
        missing=tuple(m.test_id for m in pending),
        disagreements=disagreements,
        error=error if pending else "",
    )


def _held(
    query: Callable[[str], list[dict[str, Any]]], measurement: Measurement
) -> dict[str, float]:
    """The test's authoritative series as Prometheus has them now, by metric name."""
    selector = ",".join(
        f'{key}="{_promql_escape(measurement.labels[key])}"'
        for key in ("run_id", "test_id", "direction")
    )
    held: dict[str, float] = {}
    for series in query(f'{{__name__=~"dokimi_.+",{selector}}}'):
        name = (series.get("metric") or {}).get("__name__", "")
        value = (series.get("value") or [None, None])[1]
        if name in RESULTS and value is not None:
            held[name] = float(value)
    return held


def _promql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _agree(direct: float, prometheus: float) -> bool:
    return math.isclose(direct, prometheus, rel_tol=TOLERANCE, abs_tol=1e-12)
