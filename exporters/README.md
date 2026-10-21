# Exporters

One dedicated exporter per test type, running as a sidecar alongside the test workload,
one per test pod (ADR-02-02).

An exporter parses its tool's native output, exposes the normalized metric set with the
required labels (ADR-02-03, ADR-02-07), may expose tool-specific metrics under a clearly
separated prefix, and reports completion state explicitly via
`dokimi_test_completed` so that "test failed", "test never ran" and "metric
absent" stay distinguishable.

## iperf2

The `iperf2` exporter is [`images/iperf2/dokimi_iperf2_exporter.py`](../images/iperf2/dokimi_iperf2_exporter.py).
It is Python, standard library only, and ships in the iperf2 image as
`dokimi-iperf2-exporter`, so the parser always matches the iperf2 version it parses.
`Iperf2.exporter()` says where it plugs in:
- the second container of each pod, named `exporter`;
- it serves `/metrics` on port 9101, named `metrics`, which is how the managed Prometheus
  finds it;
- it reads the `results` volume at `/results`, read-only.

**It is a regular container, not a native sidecar.** Kubernetes stops a native sidecar
once the pod's other containers have exited, and the exporter has to keep serving the
completed result after iperf2 has (ADR-03-01). It reads the files on every request, so it
serves the result for as long as the pod exists. A finished client's pod therefore stays
`Running` until the run removes it. The exporter exits at once on SIGTERM, so removing the
pod doesn't wait out its grace period.

**Each pod exports its own view**, labelled by `direction`:

| Metric | Client (`outbound`) | Server (`inbound`) |
| --- | --- | --- |
| `dokimi_test_completed` | 1 once iperf2 exited 0 with a measurement | 1 once the measured connection's report exists |
| `dokimi_execution_errors_total` | 1 if iperf2 ended without a measurement | 0 |
| `dokimi_throughput_bits_per_second` | what it sent | what it received |
| `dokimi_packet_loss_percent` | — | UDP, from the datagram counts |
| `dokimi_jitter_seconds` | — | UDP, converted from milliseconds |
| `dokimi_transferred_bytes`, `dokimi_test_duration_seconds` | ✓ | ✓ |
| `dokimi_iperf2_datagrams`, `…_lost_datagrams`, `…_out_of_order_datagrams` | — | UDP |
| `dokimi_iperf2_unparsed_lines` | ✓ | ✓ |

Results appear once `completed` is 1, as gauges in base units.

**Every series carries the full required label set** (ADR-02-07), plus `direction`:
- **from the pod's environment:** everything but the two addresses, set when the pod is
  created. The exporter refuses to start without them;
- **the addresses are the pair's own:** the source is the client and the target the server,
  whichever pod reports, so both views of one test join on the same labels.

Until a pod knows both addresses, it serves no `dokimi_` series at all, which reads as "no
result", never as a pass. The client knows them once it has been started. The server knows
them from the first connection it receives.

**What it reads.** Each pod writes its own view to the volume:
- **`iperf2.csv`**: `iperf -y C` lines. The client's is its summary of the measurement;
  the client's warm-up pass writes elsewhere. The server's has one line per connection it
  received, the warm-up's included, plus a sum line over all of them (connection id `-1`).
  Only the server's lines carry packet loss and jitter: a UDP client's own summary has
  `nan` jitter and `-1` datagrams. A line counts only once it ends in a newline, because
  the server writes while it runs and a line cut short can still look like a report.
- **`iperf2.meta`**: `role`, `warmup_seconds` and `measurement_source_port`. The client
  sends the warm-up and the measurement from two fixed source ports, so on the server the
  measured connection is the line whose source port (the third field) is
  `measurement_source_port`. The warm-up's line and the sum line are not results.
- **`iperf2.stderr`**: the client's diagnostics, next to its CSV.
- **`iperf2.peer`** and **`iperf2.status`**, the client's only: the server's address once
  it was started, and iperf2's exit status once it has finished.

**How the managed Prometheus finds an exporter** (M1-08): it discovers pods in the run's
namespace that carry the run's `dokimi.io/run-id` label, and keeps only container ports named
**`metrics`**. An exporter sidecar must therefore name its port `metrics`, which is what
`ExporterSpec.port_name` says and defaults to. The scrape uses `honor_labels: true`, so the
labels the exporter emits are never overwritten by target labels.
