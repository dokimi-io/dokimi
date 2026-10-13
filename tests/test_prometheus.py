"""The Prometheus client and phase 4's query-access check, over a scripted transport."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from dokimi.metrics.prometheus import (
    PrometheusClient,
    PrometheusError,
    PrometheusUnreachableError,
    Response,
    verify_query_access,
)

ENDPOINT = "http://run-x-prometheus.dokimi-iperf2.svc:9090"


def ok(data: object) -> Response:
    return Response(200, json.dumps({"status": "success", "data": data}).encode())


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_query_returns_the_result_vector() -> None:
    sample = {"metric": {"job": "dokimi-exporters"}, "value": [1, "1"]}
    urls: list[str] = []

    def transport(url: str) -> Response:
        urls.append(url)
        return ok({"resultType": "vector", "result": [sample]})

    assert PrometheusClient(ENDPOINT, transport=transport).query('up{job="x"}') == [sample]
    assert urls == [f"{ENDPOINT}/api/v1/query?query=up%7Bjob%3D%22x%22%7D"]


def test_an_api_error_is_raised_with_its_reason() -> None:
    def transport(url: str) -> Response:
        body = {"status": "error", "errorType": "bad_data", "error": "parse error"}
        return Response(400, json.dumps(body).encode())

    with pytest.raises(PrometheusError, match="HTTP 400: bad_data parse error"):
        PrometheusClient(ENDPOINT, transport=transport).query("up{")


def test_a_body_that_is_not_json_is_an_error_not_a_crash() -> None:
    def transport(url: str) -> Response:
        return Response(502, b"<html>Bad Gateway</html>")

    with pytest.raises(PrometheusError, match="not JSON"):
        PrometheusClient(ENDPOINT, transport=transport).query("up")


def test_targets_report_whether_each_is_up() -> None:
    def transport(url: str) -> Response:
        assert "scrapePool=dokimi-exporters" in url
        return ok(
            {
                "activeTargets": [
                    {
                        "scrapePool": "dokimi-exporters",
                        "scrapeUrl": "http://10.0.1.5:9101/metrics",
                        "health": "up",
                        "labels": {"job": "dokimi-exporters"},
                        "lastError": "",
                    },
                    {
                        "scrapePool": "dokimi-exporters",
                        "scrapeUrl": "http://10.0.2.7:9101/metrics",
                        "health": "down",
                        "labels": {},
                        "lastError": "connection refused",
                    },
                ]
            }
        )

    targets = PrometheusClient(ENDPOINT, transport=transport).targets("dokimi-exporters")

    assert [target.up for target in targets] == [True, False]
    assert targets[1].last_error == "connection refused"


def test_query_access_passes_once_a_query_is_answered() -> None:
    answers: Iterator[OSError | Response] = iter(
        [OSError("connection refused"), Response(503, b"not ready")]
    )
    clock = Clock()

    def transport(url: str) -> Response:
        if url.endswith("/-/ready"):
            answer = next(answers, Response(200, b"ready"))
            if isinstance(answer, OSError):
                raise answer
            return answer
        return ok({"resultType": "vector", "result": []})

    verify_query_access(
        PrometheusClient(ENDPOINT, transport=transport), timeout=60, clock=clock, sleep=clock.sleep
    )

    assert clock.now == 6.0  # two failed polls, three seconds apart


def test_ready_is_not_enough_the_query_must_be_answered() -> None:
    # /-/ready alone would pass a Prometheus whose query API is broken, and every later
    # read goes through the query API.
    clock = Clock()

    def transport(url: str) -> Response:
        if url.endswith("/-/ready"):
            return Response(200, b"ready")
        return Response(500, json.dumps({"status": "error", "error": "tsdb closed"}).encode())

    with pytest.raises(PrometheusUnreachableError, match="tsdb closed"):
        verify_query_access(
            PrometheusClient(ENDPOINT, transport=transport),
            timeout=10,
            clock=clock,
            sleep=clock.sleep,
        )


def test_an_unreachable_prometheus_is_a_named_preflight_failure() -> None:
    clock = Clock()

    def transport(url: str) -> Response:
        raise ConnectionRefusedError("connection refused")

    with pytest.raises(PrometheusUnreachableError) as raised:
        verify_query_access(
            PrometheusClient(ENDPOINT, transport=transport),
            timeout=10,
            clock=clock,
            sleep=clock.sleep,
        )

    message = str(raised.value)
    assert message.startswith("cluster not ready (prometheus_unreachable)")
    assert ENDPOINT in message
    assert "connection refused" in message
    assert raised.value.reason == "prometheus_unreachable"  # ADR-05-05's category
