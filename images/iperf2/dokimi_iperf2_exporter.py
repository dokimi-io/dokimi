#!/usr/bin/env python3
"""dokimi's iperf2 exporter: one pod's own view of its test, as Prometheus metrics.

It runs as the second container of each iperf2 pod (ADR-02-02), in the same image as the
tool, so the parser always matches the iperf2 version it parses. It reads what the role
script and iperf2 write to the shared results directory, and serves ``/metrics`` from it:
on every request, from the files, so it keeps serving the completed result after iperf2
has exited, for as long as the pod exists (ADR-03-01).

**Each pod exports its own view, labelled by direction** (ADR-02-02):

* the client (``direction="outbound"``) reports what it sent: its throughput;
* the server (``direction="inbound"``) reports what it received: its throughput, and for
  UDP the packet loss and the jitter, which only the receiver can see.

**The normalized set** (ADR-02-03, under ADR-01-10's ``dokimi_`` prefix) in base units,
converted here on write: bits per second, seconds, bytes. ``packet_loss_percent`` is the
named exception. Results are gauges holding the completed value. Tool-specific values
with no normalized equivalent go under ``dokimi_iperf2_``.

**Every series carries the full required label set** (ADR-02-07): ``cluster``,
``run_id``, ``test_id``, ``repetition``, ``test_type``, ``source_node``/``target_node``,
``source_pod``/``target_pod``, ``source_ip``/``target_ip`` and ``protocol``, plus
``direction``. Everything but the IP addresses is fixed when the pod is created and comes
from its environment; the exporter refuses to start without it, because a series missing
a required label could never be matched to its test. The addresses are the pair's own:
the source is the client, the target the server, whichever pod reports. Until a pod knows
both, it serves no ``dokimi_`` series at all, which reads as "no result", never as a pass.

**What the files are** (images/iperf2/dokimi-iperf2-*.sh):

* ``iperf2.meta``: ``role``, ``warmup_seconds`` and ``measurement_source_port``;
* ``iperf2.csv``: ``iperf -y C`` lines. Fields, in order: time, source address and port,
  destination address and port, connection id, interval, bytes, bits per second, and for
  UDP jitter in milliseconds, lost and total datagrams, loss percent, out of order. The
  measurement is the line whose source port is ``measurement_source_port``; the warm-up's
  line and the sum line (connection id ``-1``) are not results;
* the client also writes ``iperf2.peer``, the server's address once it is started, and
  ``iperf2.status``, iperf2's exit status once it has finished.

**Completion** (``dokimi_test_completed``): the client is complete once iperf2 exited 0
with a measurement line; a non-zero exit, or none, is a completed run that measured
nothing, so ``completed`` stays 0 and ``dokimi_execution_errors_total`` counts 1. The
server is complete once its measurement line exists: for UDP that line is the only proof
anything arrived, since a client against nothing still exits 0.

Usage::

    dokimi-iperf2-exporter --results DIR --port N
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__all__ = [
    "LABEL_ENVIRONMENT",
    "Labels",
    "Report",
    "Snapshot",
    "labels_from_environment",
    "main",
    "parse_reports",
    "read_snapshot",
    "render",
]

#: The required labels fixed at pod creation, and the variable each is given in.
LABEL_ENVIRONMENT: Mapping[str, str] = {
    "cluster": "DOKIMI_CLUSTER",
    "run_id": "DOKIMI_RUN_ID",
    "test_id": "DOKIMI_TEST_ID",
    "repetition": "DOKIMI_REPETITION",
    "test_type": "DOKIMI_TEST_TYPE",
    "source_node": "DOKIMI_SOURCE_NODE",
    "target_node": "DOKIMI_TARGET_NODE",
    "source_pod": "DOKIMI_SOURCE_POD",
    "target_pod": "DOKIMI_TARGET_POD",
    "protocol": "DOKIMI_PROTOCOL",
}
#: This pod's own address, from the downward API: what it knows before any traffic.
POD_IP_ENVIRONMENT = "DOKIMI_POD_IP"

_DIRECTION = {"client": "outbound", "server": "inbound"}
_TCP_FIELDS = 9
_UDP_FIELDS = 14
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class ExporterConfigError(Exception):
    """The pod was created without what every series must carry."""


@dataclass(frozen=True, slots=True)
class Labels:
    """The labels fixed at pod creation, validated once at start."""

    fixed: Mapping[str, str]
    pod_ip: str


@dataclass(frozen=True, slots=True)
class Report:
    """One ``iperf -y C`` line, in base units where it has a unit."""

    source_ip: str
    source_port: int
    target_ip: str
    target_port: int
    connection_id: int
    interval_seconds: float
    bytes: int
    bits_per_second: float
    #: UDP only, and only as the receiver reports them: ``None`` where iperf2 has none.
    jitter_seconds: float | None = None
    lost_datagrams: int | None = None
    datagrams: int | None = None
    loss_percent: float | None = None
    out_of_order_datagrams: int | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything one pod's results directory says, at one moment."""

    role: str
    measurement_source_port: int | None
    reports: tuple[Report, ...]
    unparsed_lines: int
    #: The client's: iperf2's exit status once it has finished, the server's address
    #: once it was started. ``None`` on the server, and before then on the client.
    exit_status: int | None = None
    peer_ip: str | None = None

    @property
    def measurement(self) -> Report | None:
        """The measured connection's line, told apart from the warm-up by its port."""
        if self.measurement_source_port is None:
            return None
        found = [
            report
            for report in self.reports
            if report.source_port == self.measurement_source_port and report.connection_id >= 0
        ]
        return found[-1] if found else None


# -- reading -------------------------------------------------------------------------------


def labels_from_environment(env: Mapping[str, str]) -> Labels:
    """The fixed labels, or :class:`ExporterConfigError` naming every one that is missing."""
    missing = [name for name in LABEL_ENVIRONMENT.values() if not env.get(name, "").strip()]
    if not env.get(POD_IP_ENVIRONMENT, "").strip():
        missing.append(POD_IP_ENVIRONMENT)
    if missing:
        raise ExporterConfigError(
            "the pod was created without " + ", ".join(missing) + ": every series must carry "
            "the full required label set (ADR-02-07), so the exporter does not start"
        )
    return Labels(
        fixed={label: env[name].strip() for label, name in LABEL_ENVIRONMENT.items()},
        pod_ip=env[POD_IP_ENVIRONMENT].strip(),
    )


def parse_reports(text: str) -> tuple[list[Report], int]:
    """Every ``iperf -y C`` line in *text*, and how many lines were not one."""
    reports: list[Report] = []
    unparsed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        report = _parse_line(line.strip())
        if report is None:
            unparsed += 1
        else:
            reports.append(report)
    return reports, unparsed


def _parse_line(line: str) -> Report | None:
    fields = line.split(",")
    if len(fields) not in (_TCP_FIELDS, _UDP_FIELDS):
        return None
    try:
        start, _, end = fields[6].partition("-")
        report = Report(
            source_ip=fields[1],
            source_port=int(fields[2]),
            target_ip=fields[3],
            target_port=int(fields[4]),
            connection_id=int(fields[5]),
            interval_seconds=float(end) - float(start),
            bytes=int(fields[7]),
            bits_per_second=float(fields[8]),
        )
        if len(fields) == _UDP_FIELDS:
            report = _with_udp(report, fields)
    except ValueError:
        return None
    return report


def _with_udp(report: Report, fields: Sequence[str]) -> Report:
    jitter_ms = float(fields[9])
    lost, total, out_of_order = int(fields[10]), int(fields[11]), int(fields[13])
    if total < 0 or lost < 0:
        # A UDP client's own summary: it cannot know what arrived.
        return report
    return replace(
        report,
        jitter_seconds=None if math.isnan(jitter_ms) else jitter_ms / 1000,
        lost_datagrams=lost,
        datagrams=total,
        # From the counts, not the tool's rounded percentage.
        loss_percent=100 * lost / total if total > 0 else float(fields[12]),
        out_of_order_datagrams=out_of_order,
    )


def read_snapshot(results: Path) -> Snapshot | None:
    """What the results directory says now, or ``None`` before the role script wrote meta."""
    meta = _read_meta(results / "iperf2.meta")
    if meta is None:
        return None
    reports, unparsed = parse_reports(_read(results / "iperf2.csv") or "")
    status = (_read(results / "iperf2.status") or "").strip()
    peer = (_read(results / "iperf2.peer") or "").strip()
    port = meta.get("measurement_source_port", "")
    return Snapshot(
        role=meta.get("role", ""),
        measurement_source_port=int(port) if port.isdigit() else None,
        reports=tuple(reports),
        unparsed_lines=unparsed,
        exit_status=int(status) if status.lstrip("-").isdigit() else None,
        peer_ip=peer or None,
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def _read_meta(path: Path) -> dict[str, str] | None:
    text = _read(path)
    if text is None:
        return None
    meta: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            meta[key.strip()] = value.strip()
    return meta


# -- rendering -----------------------------------------------------------------------------


def render(snapshot: Snapshot | None, labels: Labels) -> str:
    """The exposition text for one snapshot: empty until the pair's addresses are known."""
    if snapshot is None or snapshot.role not in _DIRECTION:
        return ""
    addresses = _addresses(snapshot, labels)
    if addresses is None:
        return ""
    series = {
        **labels.fixed,
        "source_ip": addresses[0],
        "target_ip": addresses[1],
        "direction": _DIRECTION[snapshot.role],
    }
    measurement = snapshot.measurement
    completed, errors = _completion(snapshot, measurement)

    lines: list[str] = []
    _metric(
        lines,
        "dokimi_test_completed",
        "gauge",
        "1 once this pod holds its result",
        series,
        1 if completed else 0,
    )
    _metric(
        lines,
        "dokimi_execution_errors_total",
        "counter",
        "Runs of the tool that ended without a measurement",
        series,
        errors,
    )
    if completed and measurement is not None:
        _results(lines, series, measurement)
    _metric(
        lines,
        "dokimi_iperf2_unparsed_lines",
        "gauge",
        "Lines in iperf2's output that are not an iperf -y C report",
        series,
        snapshot.unparsed_lines,
    )
    return "".join(lines)


def _addresses(snapshot: Snapshot, labels: Labels) -> tuple[str, str] | None:
    """(source, target): the client's address and the server's, as far as this pod knows."""
    measurement = snapshot.measurement
    if measurement is not None:
        return measurement.source_ip, measurement.target_ip
    if snapshot.role == "client":
        return (labels.pod_ip, snapshot.peer_ip) if snapshot.peer_ip else None
    # The server learns the client's address from its first connection, the warm-up's.
    for report in snapshot.reports:
        if report.connection_id >= 0:
            return report.source_ip, labels.pod_ip
    return None


def _completion(snapshot: Snapshot, measurement: Report | None) -> tuple[bool, int]:
    if snapshot.role == "server":
        return measurement is not None, 0
    if snapshot.exit_status is None:
        return False, 0
    if snapshot.exit_status == 0 and measurement is not None:
        return True, 0
    return False, 1


def _results(lines: list[str], series: Mapping[str, str], report: Report) -> None:
    _metric(
        lines,
        "dokimi_throughput_bits_per_second",
        "gauge",
        "Throughput this pod observed, in bits per second",
        series,
        report.bits_per_second,
    )
    if report.loss_percent is not None:
        _metric(
            lines,
            "dokimi_packet_loss_percent",
            "gauge",
            "Datagrams lost on the way, in percent of those sent",
            series,
            report.loss_percent,
        )
    if report.jitter_seconds is not None:
        _metric(
            lines,
            "dokimi_jitter_seconds",
            "gauge",
            "Packet delay variation the receiver observed, in seconds",
            series,
            report.jitter_seconds,
        )
    _metric(
        lines,
        "dokimi_iperf2_transferred_bytes",
        "gauge",
        "Bytes iperf2 reports for the measurement",
        series,
        report.bytes,
    )
    _metric(
        lines,
        "dokimi_iperf2_interval_seconds",
        "gauge",
        "The interval iperf2's report covers, in seconds",
        series,
        report.interval_seconds,
    )
    if report.datagrams is not None:
        _metric(
            lines,
            "dokimi_iperf2_datagrams",
            "gauge",
            "Datagrams the receiver expected",
            series,
            report.datagrams,
        )
        _metric(
            lines,
            "dokimi_iperf2_lost_datagrams",
            "gauge",
            "Datagrams the receiver did not get",
            series,
            report.lost_datagrams or 0,
        )
        _metric(
            lines,
            "dokimi_iperf2_out_of_order_datagrams",
            "gauge",
            "Datagrams the receiver got out of order",
            series,
            report.out_of_order_datagrams or 0,
        )


def _metric(
    lines: list[str],
    name: str,
    kind: str,
    help_text: str,
    labels: Mapping[str, str],
    value: float,
) -> None:
    lines.append(f"# HELP {name} {help_text}\n")
    lines.append(f"# TYPE {name} {kind}\n")
    rendered = ",".join(f'{key}="{_escape(text)}"' for key, text in sorted(labels.items()))
    lines.append(f"{name}{{{rendered}}} {_number(value)}\n")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return repr(float(value))


# -- serving -------------------------------------------------------------------------------


def _handler(results: Path, labels: Labels) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            body = render(read_snapshot(results), labels).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", _CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            # A scrape every few seconds is not news; the pod's log is the tool's.
            return

    return Handler


def serve(results: Path, port: int, labels: Labels) -> ThreadingHTTPServer:
    """A server for ``/metrics`` on *port*, not yet started."""
    return ThreadingHTTPServer(("", port), _handler(results, labels))


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dokimi-iperf2-exporter")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        labels = labels_from_environment(os.environ if env is None else env)
    except ExporterConfigError as error:
        print(f"dokimi-iperf2-exporter: {error}", file=sys.stderr)
        return 64
    server = serve(args.results, args.port, labels)
    print(f"exporter: serving /metrics on {args.port} from {args.results}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
