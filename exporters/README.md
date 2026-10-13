# Exporters

One dedicated exporter per test type, running as a sidecar alongside the test workload,
one per test pod (ADR-02-02).

An exporter parses its tool's native output, exposes the normalized metric set with the
required labels (ADR-02-03, ADR-02-04), may expose tool-specific metrics under a clearly
separated prefix, and reports completion state explicitly via
`dokimi_test_completed` so that "test failed", "test never ran" and "metric
absent" stay distinguishable.

The first exporter, for `iperf2`, arrives with M1-10 (adrs#45). M1-09 already fixed where
it plugs in, in `Iperf2.exporter()`: a sidecar container named `exporter`, serving
`/metrics` on port 9101, reading the tool's output from the `results` volume at
`/results`, which both pods already mount. Each pod writes its own view there:

- **`iperf2.csv`**: `iperf -y C` lines. The client's is its summary of the measurement;
  the client's warm-up pass writes elsewhere. The server's has one line per connection it
  received, the warm-up's included, plus a sum line over all of them (connection id `-1`).
  Only the server's lines carry packet loss and jitter: a UDP client's own summary has
  `nan` jitter and `-1` datagrams.
- **`iperf2.meta`**: `role`, `warmup_seconds` and `measurement_source_port`. The client
  sends the warm-up and the measurement from two fixed source ports, so on the server the
  measured connection is the line whose source port (the third field) is
  `measurement_source_port`. The warm-up's line and the sum line are not results.
- **`iperf2.stderr`**: the client's diagnostics, next to its CSV.

**How the managed Prometheus finds an exporter** (M1-08): it discovers pods in the run's
namespace that carry the run's `dokimi.io/run-id` label, and keeps only container ports named
**`metrics`**. An exporter sidecar must therefore name its port `metrics`, which is what
`ExporterSpec.port_name` says and defaults to. The scrape uses `honor_labels: true`, so the
labels the exporter emits are never overwritten by target labels.
