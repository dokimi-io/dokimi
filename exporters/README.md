# Exporters

One dedicated exporter per test type, running as a sidecar alongside the test workload,
one per test pod (ADR-02-02).

An exporter parses its tool's native output, exposes the normalized metric set with the
required labels (ADR-02-03, ADR-02-04), may expose tool-specific metrics under a clearly
separated prefix, and reports completion state explicitly via
`test_orchestrator_test_completed` so that "test failed", "test never ran" and "metric
absent" stay distinguishable.

The first exporter, for `iperf2`, arrives with M1-10 (adrs#45).
