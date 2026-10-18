"""Phase 7 (M1-12, ADR-03-01): the direct read, and Prometheus's confirmation of it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dokimi.measurement import (
    AUTHORITATIVE_DIRECTION,
    TOLERANCE,
    ExporterParseError,
    Measurement,
    MetricsMissingError,
    confirm_scrape,
    measure,
    parse_exposition,
    read_measurement,
    scrape_confirmation_timeout,
    write_measurement,
)
from tests.fake_exporter import INBOUND_RESULT, exposition, labels, pair_serves

RUN_ID = "20261018-120000-m3asur"
SERVER = "http://10.244.1.5:9101/metrics"
CLIENT = "http://10.244.2.7:9101/metrics"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def read(read_metrics: Any, *, protocol: str = "udp", timeout: float = 30.0) -> Measurement:
    clock = Clock()
    return measure(
        [SERVER, CLIENT],
        run_id=RUN_ID,
        test_id="pair-001",
        protocol=protocol,
        read=read_metrics,
        timeout=timeout,
        clock=clock,
        sleep=clock.sleep,
    )


# -- parsing ------------------------------------------------------------------------------


def test_the_exposition_format_is_read_as_the_exporter_writes_it() -> None:
    text = exposition(labels("inbound", run_id=RUN_ID), results=INBOUND_RESULT)

    samples = parse_exposition(text)

    throughput = next(s for s in samples if s.name == "dokimi_throughput_bits_per_second")
    assert throughput.value == 49983531.0
    assert throughput.labels["direction"] == "inbound"
    assert throughput.labels["run_id"] == RUN_ID


def test_escaped_label_values_are_unescaped() -> None:
    (sample,) = parse_exposition('dokimi_x{a="q\\"b\\\\c\\nd"} 1\n')

    assert sample.labels == {"a": 'q"b\\c\nd'}


@pytest.mark.parametrize(
    "line",
    ["dokimi_test_completed{run_id=", 'dokimi_x{a="b"} not-a-number', "{} 1"],
)
def test_an_unreadable_line_is_exporter_parse(line: str) -> None:
    # Acceptance criterion: an unparseable result yields exporter_parse.
    with pytest.raises(ExporterParseError) as raised:
        parse_exposition(line + "\n", test_id="pair-001")

    assert raised.value.category == "exporter_parse"


# -- the direct read ----------------------------------------------------------------------


def test_the_receivers_view_is_the_result() -> None:
    measurement = read(pair_serves(run_id=RUN_ID))

    assert measurement.direction == AUTHORITATIVE_DIRECTION == "inbound"
    assert measurement.values == INBOUND_RESULT
    assert measurement.source == "direct"
    assert measurement.labels["target_pod"].endswith("-server")


def test_tcp_has_no_loss_or_jitter_and_needs_none() -> None:
    measurement = read(pair_serves(run_id=RUN_ID, protocol="tcp"), protocol="tcp")

    assert "dokimi_packet_loss_percent" not in measurement.values
    assert measurement.values["dokimi_throughput_bits_per_second"] == 49983531.0


def test_an_exporter_serving_no_result_is_metrics_missing_never_a_pass() -> None:
    # Acceptance criterion: an exporter serving no result yields metrics_missing.
    with pytest.raises(MetricsMissingError, match="no completed result from the inbound view"):
        read(pair_serves(run_id=RUN_ID, server_completed=0), timeout=3)


def test_a_result_that_completes_a_moment_later_is_read_again() -> None:
    # The receiver's line can trail the sender's exit by a moment.
    late, ready = pair_serves(run_id=RUN_ID, server_completed=0), pair_serves(run_id=RUN_ID)
    calls: list[str] = []

    def read_metrics(url: str) -> str:
        calls.append(url)
        return (late if len(calls) <= 2 else ready)(url)

    assert read(read_metrics).values == INBOUND_RESULT
    assert len(calls) == 4


def test_a_complete_view_missing_a_result_is_metrics_missing() -> None:
    without_loss = {k: v for k, v in INBOUND_RESULT.items() if k != "dokimi_packet_loss_percent"}

    with pytest.raises(MetricsMissingError, match="serves no dokimi_packet_loss_percent"):
        read(pair_serves(run_id=RUN_ID, inbound=without_loss))


def test_another_tests_series_are_not_this_tests_result() -> None:
    with pytest.raises(MetricsMissingError):
        read(pair_serves(run_id=RUN_ID, test_id="pair-002"), timeout=2)


def test_an_unreachable_exporter_is_metrics_missing() -> None:
    def refused(url: str) -> str:
        raise ConnectionRefusedError("connection refused")

    with pytest.raises(MetricsMissingError, match="could not read http://10.244.1.5"):
        read(refused)


def test_a_series_without_a_required_label_is_exporter_parse() -> None:
    # ADR-02-07: a missing required label is an exporter defect.
    server_labels = labels("inbound", run_id=RUN_ID)
    del server_labels["cluster"]
    server = exposition(server_labels, results=INBOUND_RESULT)
    client = pair_serves(run_id=RUN_ID)

    with pytest.raises(ExporterParseError, match="lacks the required label.s. cluster"):
        read(lambda url: server if url == SERVER else client(url))


def test_the_measurement_round_trips_through_its_file(tmp_path: Path) -> None:
    measurement = read(pair_serves(run_id=RUN_ID))

    path = write_measurement(measurement, tmp_path)

    assert path.name == "pair-001.measurement.json"
    assert read_measurement(tmp_path, "pair-001") == measurement
    assert read_measurement(tmp_path, "pair-002") is None


# -- the scrape confirmation --------------------------------------------------------------


def held(measurement: Measurement, **changed: float) -> list[dict[str, Any]]:
    """Prometheus's vector for the test's series, values as strings, as its API sends them."""
    values = {**measurement.values, **changed}
    return [
        {"metric": {"__name__": name, **measurement.labels}, "value": [1792334400.0, repr(v)]}
        for name, v in values.items()
    ]


def confirm(query: Any, measurement: Measurement, **kwargs: Any) -> Any:
    clock = Clock()
    return confirm_scrape(
        query, [measurement], scrape_interval=5, clock=clock, sleep=clock.sleep, **kwargs
    )


def test_the_timeout_is_three_scrape_intervals_and_at_least_a_minute() -> None:
    assert scrape_confirmation_timeout(5) == 60
    assert scrape_confirmation_timeout(30) == 90


def test_series_that_arrived_with_the_same_values_are_confirmed() -> None:
    measurement = read(pair_serves(run_id=RUN_ID))
    queries: list[str] = []

    def query(expression: str) -> list[dict[str, Any]]:
        queries.append(expression)
        return held(measurement)

    report = confirm(query, measurement)

    assert report.confirmed == {"pair-001": dict(measurement.values)}
    assert report.missing == ()
    assert report.disagreements == ()
    assert f'run_id="{RUN_ID}"' in queries[0]
    assert 'test_id="pair-001"' in queries[0]
    assert 'direction="inbound"' in queries[0]


def test_series_that_arrive_late_are_waited_for() -> None:
    measurement = read(pair_serves(run_id=RUN_ID))
    answers = [[], [], held(measurement)]

    report = confirm(lambda _expression: answers.pop(0), measurement)

    assert "pair-001" in report.confirmed


def test_a_blocked_scrape_is_a_warning_and_the_verdict_stands() -> None:
    # Acceptance criterion: a deliberately blocked scrape produces a warning, not a failure.
    measurement = read(pair_serves(run_id=RUN_ID))

    report = confirm(lambda _expression: [], measurement)

    assert report.missing == ("pair-001",)
    assert report.confirmed == {}
    assert report.timeout == 60
    document = json.loads(report.to_json([measurement]))
    assert document["tests"][0]["history_captured"] is False
    # The direct read is still the result.
    assert document["tests"][0]["values"] == INBOUND_RESULT


def test_a_prometheus_that_cannot_be_asked_confirms_nothing_and_says_why() -> None:
    measurement = read(pair_serves(run_id=RUN_ID))

    def refused(_expression: str) -> list[dict[str, Any]]:
        raise ConnectionRefusedError("connection refused")

    report = confirm(refused, measurement, timeout=10)

    assert report.missing == ("pair-001",)
    assert "connection refused" in report.error


def test_a_value_that_differs_is_a_diagnostic_of_its_own() -> None:
    measurement = read(pair_serves(run_id=RUN_ID))

    report = confirm(
        lambda _e: held(measurement, dokimi_throughput_bits_per_second=49_000_000.0), measurement
    )

    (disagreement,) = report.disagreements
    assert disagreement.metric == "dokimi_throughput_bits_per_second"
    assert (disagreement.direct, disagreement.prometheus) == (49983531.0, 49_000_000.0)
    document = json.loads(report.to_json([measurement]))
    assert document["tests"][0]["disagreements"][0]["metric"] == disagreement.metric
    assert document["tolerance"] == TOLERANCE


def test_a_floats_rounding_is_no_disagreement() -> None:
    measurement = read(pair_serves(run_id=RUN_ID))
    jitter = measurement.values["dokimi_jitter_seconds"]

    report = confirm(
        lambda _e: held(measurement, dokimi_jitter_seconds=jitter * (1 + 1e-12)), measurement
    )

    assert report.disagreements == ()
