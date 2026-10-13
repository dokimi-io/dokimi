"""The Prometheus HTTP API, as much of it as a run needs, and the query-access check.

[ADR-01-04] makes Prometheus the common query interface; [ADR-03-02] requires query
access to be verified **before any workload is deployed**, so that a monitoring problem
fails the run in phase 4 rather than after a full test cycle. :func:`verify_query_access`
is that check. It fails with :class:`PrometheusUnreachableError`, a run-level preflight
failure: the run exits ``4`` with one clear message, and nothing is measured
([ADR-05-07], [ADR-01-11]).

The HTTP client is the standard library's. Three read-only GET endpoints do not justify a
dependency, and the transport is injected so every path here is testable without a
network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_QUERY_ACCESS_TIMEOUT_SECONDS",
    "PrometheusClient",
    "PrometheusError",
    "PrometheusUnreachableError",
    "Response",
    "Target",
    "Transport",
    "urllib_transport",
    "verify_query_access",
]

#: How long phase 4 waits for Prometheus to answer a query. A managed instance has to be
#: scheduled, pull its image and replay an empty WAL within this; it is well inside the
#: Job budget's preflight term (``DEFAULT_PREFLIGHT_SECONDS``).
DEFAULT_QUERY_ACCESS_TIMEOUT_SECONDS = 180.0
_POLL_INTERVAL_SECONDS = 3.0
_REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes


#: GET *url*, returning the response or raising ``OSError`` if nothing answered.
Transport = Callable[[str], Response]


def urllib_transport(url: str) -> Response:
    """The real transport. An HTTP error status is a response, not an exception."""
    try:
        with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT_SECONDS) as reply:  # noqa: S310
            return Response(reply.status, reply.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, error.read() or b"")


class PrometheusError(RuntimeError):
    """Prometheus answered, but not with what the API promises."""


class PrometheusUnreachableError(RuntimeError):
    """Prometheus could not be queried in phase 4: a run-level preflight failure.

    ADR-05-05's ``prometheus_unreachable`` category. Raised before any workload exists,
    so the run exits ``4`` with nothing to clean up but the Prometheus itself.
    """

    reason = "prometheus_unreachable"

    def __init__(self, endpoint: str, detail: str) -> None:
        self.endpoint = endpoint
        self.detail = detail
        super().__init__(
            f"cluster not ready ({self.reason}): Prometheus at {endpoint} could not be "
            f"queried: {detail}"
        )


@dataclass(frozen=True, slots=True)
class Target:
    """One scrape target, as ``/api/v1/targets`` reports it."""

    scrape_pool: str
    url: str
    health: str
    labels: Mapping[str, str]
    last_error: str

    @property
    def up(self) -> bool:
        return self.health == "up"


class PrometheusClient:
    """Read-only access to one Prometheus, by its base URL."""

    def __init__(self, endpoint: str, *, transport: Transport | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._transport = transport if transport is not None else urllib_transport

    def ready(self) -> bool:
        """``/-/ready``: the TSDB is open and the server is accepting queries."""
        return self._transport(f"{self.endpoint}/-/ready").status == 200

    def query(self, expression: str) -> list[dict[str, Any]]:
        """An instant query. Returns the result vector; an API error raises."""
        data = self._get("/api/v1/query", {"query": expression})
        if data.get("resultType") != "vector":
            raise PrometheusError(f"expected a vector for {expression!r}, got {data!r}")
        result: list[dict[str, Any]] = data.get("result") or []
        return result

    def targets(self, scrape_pool: str | None = None) -> list[Target]:
        """The active scrape targets, optionally of one scrape pool (one job)."""
        params = {"state": "active"}
        if scrape_pool is not None:
            params["scrapePool"] = scrape_pool
        data = self._get("/api/v1/targets", params)
        return [
            Target(
                scrape_pool=target.get("scrapePool", ""),
                url=target.get("scrapeUrl", ""),
                health=target.get("health", "unknown"),
                labels=target.get("labels") or {},
                last_error=target.get("lastError", ""),
            )
            for target in data.get("activeTargets") or []
        ]

    def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        url = f"{self.endpoint}{path}?{urllib.parse.urlencode(params)}"
        response = self._transport(url)
        try:
            payload = json.loads(response.body)
        except ValueError:
            raise PrometheusError(
                f"{path} answered HTTP {response.status} with a body that is not JSON"
            ) from None
        if response.status != 200 or payload.get("status") != "success":
            raise PrometheusError(
                f"{path} answered HTTP {response.status}: "
                f"{payload.get('errorType', '')} {payload.get('error', '')}".strip()
            )
        data: dict[str, Any] = payload.get("data") or {}
        return data


def verify_query_access(
    client: PrometheusClient,
    *,
    timeout: float = DEFAULT_QUERY_ACCESS_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait until *client*'s Prometheus answers a query, or raise.

    "Answers a query" is the bar, not "is reachable": an instant query of ``up`` goes
    through the query engine and the TSDB, which is exactly the path every later
    measurement read takes. The error names the last thing that went wrong, because
    "connection refused", "HTTP 503" and "not JSON" point at different problems.
    """
    deadline = clock() + timeout
    last = "no answer yet"
    while True:
        try:
            if client.ready():
                client.query("up")
                return
            last = "/-/ready did not report ready"
        except (OSError, PrometheusError) as error:
            last = str(error) or type(error).__name__
        if clock() >= deadline:
            raise PrometheusUnreachableError(
                client.endpoint, f"{last} (gave up after {timeout:g}s)"
            )
        sleep(_POLL_INTERVAL_SECONDS)
