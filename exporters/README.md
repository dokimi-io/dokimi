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
`/results`, which both pods already mount. The client writes `iperf2.csv` there (`iperf -y
C`) and `iperf2.stderr` next to it. The warm-up pass writes elsewhere, so the exporter never
sees a number that was meant to be discarded.
