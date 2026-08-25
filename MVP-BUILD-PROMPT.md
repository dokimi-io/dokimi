# Prompt: build the Milestone-01 MVP

This is a draft prompt for a coding agent, not yet run. Review and refine it before using it.

---

## Context to give the agent

You are implementing Milestone-01 of `test-orchestrator`, a Kubernetes network-test orchestration
framework. The full decided architecture is in the sibling repository `../test-orchestrator-adrs/`
(ADRs) and summarized in this repository's `README.md`. Read both before writing code — the README
links every claim back to the ADR that decided it, and any implementation choice not covered by an
accepted ADR should be flagged rather than guessed.

**Do not implement anything beyond Milestone-01's scope.** Test matrices beyond one pair, node
selection by label, multiple test types, bounded concurrency, a result database, and stable
(`v1`) interfaces are all real, decided architecture — they belong to later milestones and adding
them now is scope creep the roadmap deliberately defers.

## The execution engine is pure Python — no Robot Framework at runtime

Settled by the accepted
[ADR-04-05](../test-orchestrator-adrs/adrs/04-05-canonical-test-definition-model-with-pluggable-formats.md):
the orchestration library consumes the canonical test-definition model only and "must not know that
a suite exists... must not know that YAML exists." Robot Framework is a `parse()`/`render()` client
format against that model, not something the Job invokes to run or validate a test — build the
Job's execution and pass-criteria evaluation directly against
[ADR-02-05](../test-orchestrator-adrs/adrs/02-05-human-readable-validation-keywords-mapped-to-promql.md)'s
canonical criterion record in Python.

For Milestone-01 this is moot in practice: the CLI takes its parameters directly (two node names,
bandwidth, duration, thresholds — see Deliverable 2 below), so there is no Robot suite or YAML file
to parse at all yet. Format parsing (`parse`/`render` for Robot and YAML) is Milestone-02 build-out
against ADR-04-03's schema, not part of this prompt's scope.

## Milestone-01 scope (exact)

From [ROADMAP.md](../test-orchestrator-adrs/ROADMAP.md#milestone-01--first-end-to-end-mvp):

- A local CLI validates input and submits the one-shot orchestration Job.
- The Job discovers one cluster and resolves two explicitly named nodes.
- One directed `iperf2` UDP test runs sequentially over the pod network.
- A managed Prometheus instance observes completion, throughput, and packet loss.
- The orchestrator evaluates minimum throughput and maximum packet loss.
- The run reports pass, fail, or error, captures its minimal diagnostic record, and safely cleans
  up run-owned resources.

## Deliverables

### 1. Repository layout and packaging

- Single `uv.lock` at the repository root; the CLI distribution and the orchestrator container
  image both build from it
  ([ADR-01-02](../test-orchestrator-adrs/adrs/01-02-python-as-the-orchestration-implementation-language.md)).
- Python 3.12+, `kubernetes` client pinned to 1.35+.
- Structure the orchestration logic as a library with no test-framework-specific imports in its
  core — the CLI is a thin adapter over it, so a future Operator or CI entry point can be another
  one without a rewrite
  ([ADR-01-06](../test-orchestrator-adrs/adrs/01-06-orchestrator-as-a-one-shot-in-cluster-job.md)).

### 2. Local CLI

- Accepts run parameters directly (two node names, bandwidth, duration, thresholds) — Milestone-01
  has no YAML schema or config precedence yet
  ([ADR-04-02](../test-orchestrator-adrs/adrs/04-02-configuration-sources-and-precedence.md) and
  [ADR-04-03](../test-orchestrator-adrs/adrs/04-03-versioned-yaml-configuration-schema.md) are both
  `proposed`, Milestone-02 — don't build them now).
- `--dry-run` renders every manifest the run would need locally and submits nothing to the
  cluster.
- Submits the orchestration Job, streams its logs, and can reattach to an already-running Job by
  its `run_id` ownership label if disconnected.

### 3. Orchestrator Job (in-cluster)

In order:

1. **Run-level preflight**
   ([ADR-05-07](../test-orchestrator-adrs/adrs/05-07-cluster-preflight-and-pod-readiness-checks.md)):
   scheduling, pod-to-pod CNI connectivity, DNS resolution, image pull, and available-node-count
   checks, reusing the first scheduled test pair rather than a synthetic one. Any failure aborts
   immediately with one clear "cluster not ready" error, before any per-test error category would
   apply.
2. **Deploy** the `iperf2` client/server workload pair and their sidecar exporters, labeled
   run-owned
   ([ADR-05-02](../test-orchestrator-adrs/adrs/05-02-run-scoped-resource-ownership-and-cleanup.md)),
   using the Alpine-based, digest-pinned images from
   [ADR-05-04](../test-orchestrator-adrs/adrs/05-04-container-image-strategy-for-test-workloads.md).
   Deploy or reuse Prometheus per the `auto` mode default
   ([ADR-03-02](../test-orchestrator-adrs/adrs/03-02-prometheus-integration-modes.md)).
3. **Per-test readiness gate**: wait for both pods `Ready` via the Kubernetes API before calling
   `start()`; a pod that never becomes `Ready` is a `scheduling` error for that test.
4. **Discarded warm-up pass**, then the measured run
   ([ADR-04-04](../test-orchestrator-adrs/adrs/04-04-test-execution-concurrency-strategy.md)) — a
   short, conservative default duration, recorded in run metadata (`warmup_duration_seconds`),
   configurable to disable.
5. **Read the verdict directly from the exporter's `/metrics` endpoint** once the workload reports
   completion; Prometheus scrapes the same endpoint for history only. Absence of a direct read is
   an `error`, never a `pass`. Confirm the scrape landed before teardown (timeout ≈
   `3 × scrape_interval`, minimum 60s) and warn — do not fail the run — if it didn't
   ([ADR-03-01](../test-orchestrator-adrs/adrs/03-01-metric-delivery-for-short-lived-tests.md)).
6. **Evaluate** `Throughput Should Be At Least <bandwidth>` and
   `Packet Loss Should Be At Most 0 percent` against the directly-read values, using the canonical
   criterion record (`metric`, `comparison`, `threshold`, `unit`)
   ([ADR-02-05](../test-orchestrator-adrs/adrs/02-05-human-readable-validation-keywords-mapped-to-promql.md)).
   Classify the outcome per the table in
   [ADR-05-05](../test-orchestrator-adrs/adrs/05-05-failure-and-error-categories.md)
   (`pass`/`fail`/`error`; `config_error`/`skipped` exist but Milestone-01 has no path that
   produces them yet).
7. **Post-run diagnostic snapshot** before cleanup: pod-to-node placement, each pod's final
   status, whether the namespace/Prometheus were still reachable.
8. **Write the result artifact**: CSV and JSONL, both, automatically, two grains (per-test,
   per-assertion), from the same values used for the verdict, never re-derived
   ([ADR-05-09](../test-orchestrator-adrs/adrs/05-09-milestone-01-local-result-artifact.md)). No
   database write in Milestone-01 — that path is optional and Milestone-04 scope.
9. **Clean up** every run-owned resource; retain on failure; report cleanup failures separately
   from the test outcome, never silently.

### 4. Container images

- Test-workload/exporter image(s): `ghcr.io/test-orchestrator/iperf2*`, Alpine-based, digest-pinned,
  signed.
- Orchestrator's own image: `ghcr.io/test-orchestrator/orchestrator`, `python:3.12-slim`,
  digest-pinned, signed, built via `uv sync --locked` against the repo-root `uv.lock`
  ([ADR-05-08](../test-orchestrator-adrs/adrs/05-08-container-image-strategy-for-the-orchestrator-itself.md)).
  The CLI release process bakes in the digest of the image built from the same commit as its
  default; both are overridable.

## Explicitly out of scope for this prompt

- Any test matrix mode beyond one directed pair (`all-to-all`, node selection by label, etc.) —
  Milestone-02.
- `iperf3`, IRTT, or any test type beyond `iperf2` UDP — Milestone-03.
- Bounded/unbounded concurrency strategy selection — moot with one pair; Milestone-02 when a
  matrix exists.
- A relational database, supporting/infrastructure metric persistence, retention policy —
  Milestone-04.
- `v1` interface freeze, deprecation handling — Milestone-05. Everything Milestone-01 builds is
  `v1alpha1`.
- Retry-on-error (`retry: true`), `optional: true` exclusions — real ADR-05-05 decisions, but
  nothing in Milestone-01's single-pair, no-repetition scope exercises them; implement the
  category taxonomy, skip the retry/optional machinery unless the agent judges it trivially cheap
  to include.

## Acceptance criteria

- Running the CLI against a real (or kind/minikube) two-node cluster with no Prometheus installed:
  a managed Prometheus is deployed, the `iperf2` pair runs, throughput/packet-loss are evaluated,
  a CSV and a JSONL file are written, and every run-owned resource is gone afterward.
- Running the CLI against a cluster with an existing compatible Prometheus: `auto` mode detects
  and uses it, deploys no second Prometheus, and does not remove it on cleanup.
- Killing the CLI mid-run and re-invoking with `--reattach <run_id>` (or equivalent) resumes log
  streaming from the still-running Job.
- A run against a cluster with broken pod-to-pod connectivity fails at the preflight step with one
  clear error, not 380 identical per-pair failures (trivially checkable with the one Milestone-01
  pair, but the preflight check itself should not be pair-count-shaped).
- `--dry-run` creates nothing in the cluster and renders the same manifests the real run would
  submit.
- The CSV and JSONL outputs contain both a per-test record and a per-assertion record for the one
  test run, matching the fields in
  [ADR-05-09](../test-orchestrator-adrs/adrs/05-09-milestone-01-local-result-artifact.md).
