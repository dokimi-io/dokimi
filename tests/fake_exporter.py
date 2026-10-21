"""What a pair's exporters serve, as text, for tests of the direct read (M1-12).

The shape is the iperf2 exporter's (images/iperf2/dokimi_iperf2_exporter.py): every
series with ADR-02-07's labels plus ``direction``, the receiver with UDP's loss and jitter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

#: The receiver's result in base units: what the direct read returns for a UDP test.
INBOUND_RESULT = {
    "dokimi_throughput_bits_per_second": 49983531.0,
    "dokimi_transferred_bytes": 31249260.0,
    "dokimi_test_duration_seconds": 5.0,
    "dokimi_packet_loss_percent": 0.0,
    "dokimi_jitter_seconds": 0.000011,
}
#: The sender's: no loss and no jitter, which it cannot see.
OUTBOUND_RESULT = {
    "dokimi_throughput_bits_per_second": 50001168.0,
    "dokimi_transferred_bytes": 31250730.0,
    "dokimi_test_duration_seconds": 5.0,
}


def labels(
    direction: str,
    *,
    run_id: str,
    test_id: str = "pair-001",
    protocol: str = "udp",
    client_ip: str = "10.244.2.7",
    server_ip: str = "10.244.1.5",
) -> dict[str, str]:
    return {
        "cluster": "test-cluster",
        "run_id": run_id,
        "test_id": test_id,
        "repetition": "1",
        "test_type": "iperf2",
        "source_node": "worker-01",
        "target_node": "worker-02",
        "source_pod": f"run-{run_id}-{test_id}-client",
        "target_pod": f"run-{run_id}-{test_id}-server",
        "source_ip": client_ip,
        "target_ip": server_ip,
        "protocol": protocol,
        "direction": direction,
    }


def exposition(
    series_labels: Mapping[str, str],
    *,
    completed: int = 1,
    results: Mapping[str, float] | None = None,
) -> str:
    """One pod's ``/metrics``: completion, and its results once completed."""
    rendered = ",".join(f'{key}="{value}"' for key, value in sorted(series_labels.items()))
    lines = [
        "# HELP dokimi_test_completed 1 once this pod holds its result",
        "# TYPE dokimi_test_completed gauge",
        f"dokimi_test_completed{{{rendered}}} {completed}",
        f"dokimi_execution_errors_total{{{rendered}}} 0",
    ]
    if completed:
        lines += [f"{name}{{{rendered}}} {value!r}" for name, value in (results or {}).items()]
    lines.append(f"dokimi_iperf2_unparsed_lines{{{rendered}}} 0")
    return "\n".join(lines) + "\n"


def pair_serves(
    *,
    run_id: str,
    test_id: str = "pair-001",
    protocol: str = "udp",
    server_ip: str = "10.244.1.5",
    client_ip: str = "10.244.2.7",
    inbound: Mapping[str, float] | None = None,
    server_completed: int = 1,
) -> Callable[[str], str]:
    """A ``read_metrics`` that answers for the pair's two exporters, by pod address."""
    common = {"run_id": run_id, "test_id": test_id, "protocol": protocol}
    addresses = {"client_ip": client_ip, "server_ip": server_ip}
    outbound = {k: v for k, v in OUTBOUND_RESULT.items()}
    received = dict(INBOUND_RESULT if inbound is None else inbound)
    if protocol == "tcp":
        for name in ("dokimi_packet_loss_percent", "dokimi_jitter_seconds"):
            received.pop(name, None)
    server = exposition(
        labels("inbound", **common, **addresses), completed=server_completed, results=received
    )
    client = exposition(labels("outbound", **common, **addresses), results=outbound)

    def read(url: str) -> str:
        return server if f"//{server_ip}:" in url else client

    return read
