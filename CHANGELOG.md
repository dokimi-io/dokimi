# Changelog

What each release brings. Versions follow semantic versioning; before 1.0, a minor version may
still change an interface, and its entry here says so.

## 0.1.0 — 2026-11-25

The first release: one network performance test, from the definition you write to a verdict CI
can act on.

**Writing and running a test**

- A test definition in YAML or as a Robot Framework suite, strict about both: anything dokimi
  doesn't understand is refused, saying where and why. `dokimi validate` checks one without a
  cluster, and `dokimi convert` prints it in the other format.
- One directed `iperf2` test, UDP or TCP, between two nodes you name, over the pod network, with
  a warm-up pass before the measured one.
- `dokimi submit` runs it as a one-shot Kubernetes Job and streams its log. `--dry-run` renders
  the plan and the manifests and creates nothing. `dokimi attach` picks a run up again, from any
  machine.
- One run at a time per cluster: a run claims the cluster before it measures, because two runs
  would compete for the network they measure.
- A preflight before any test: the nodes, scheduling, image pull, pod-to-pod connectivity and
  DNS, with the test's own pods. `dokimi preflight` runs only that.

**Measuring, and the verdict**

- A Prometheus of the run's own, with a node-exporter when the cluster has none. It is kept
  after the run, and `dokimi prometheus <run-id>` opens it on a local port.
- The result is read directly from each test pod's exporter, the receiver's view for UDP, and
  checked against what Prometheus holds.
- Pass criteria with their units, such as `Throughput Should Be At Least 400 megabits per
  second`, evaluated for each pair: pass, fail, or error when a pair couldn't be measured. No
  data is never a pass.
- The exit code is the verdict: `0` pass, `1` fail, `2` error. `3` is a configuration error,
  `4` a cluster that isn't ready, `5` an internal error.

**Reports and cleanup**

- Robot Framework's log and report, and the result files: one record per test and one per
  criterion, as CSV and JSONL, and the run's own record, `run.json`. They come back to your
  machine with the run's log.
- A snapshot of where every pod ran and how it ended, taken while that evidence exists.
- A run removes what it deployed. One that didn't pass keeps its evidence for you to look at,
  without holding the cluster, until `dokimi cleanup` removes it. `--retain` chooses
  otherwise.
- Tab completion for bash, zsh and fish: `dokimi completion <shell>`.

**Installing**

- On PyPI as `dokimi-io`: `uv tool install --python 3.14 'dokimi-io[robot]'` gives the `dokimi`
  command, with Robot Framework for `.robot` tests. Without `[robot]`, it reads tests written in
  YAML. The release also carries the wheel.

**Images**

- The orchestrator and `iperf2` images, for amd64 and arm64, signed, and pinned by digest in the
  released CLI, so a run uses the images it was released with.

**Not in 0.1:** tests across many nodes at once, other test types, latency, and a result
database. The [roadmap](https://dokimi.io/roadmap/) shows what comes next.
