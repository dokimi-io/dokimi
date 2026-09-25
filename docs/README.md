# Documentation

Four guides, in the order you are likely to need them, and the test environment's own README.

| | |
| --- | --- |
| [Installation](installation.md) | Getting the CLI onto a machine, and what it needs |
| [Usage](usage.md) | Every command, every flag, and what the exit codes mean |
| [Setup](setup.md) | A development environment, including a throwaway cluster |
| [Test environment](../test-env/README.md) | The multi-node cluster: its profiles, what `check` verifies, and what its numbers do not mean |
| [Building](building.md) | The distribution, the container image, and the release |

Two documents sit outside this folder and are the ones to read first if you want to know
*why* rather than *how*:

- The [repository README](../README.md) — the execution model, the phases of a run, the
  metric contract and the interfaces.
- The [`test-orchestrator-adrs`](https://github.com/Test-Orchestrator/test-orchestrator-adrs)
  repository — every architectural decision, with the alternatives that were rejected and
  the reasons. These guides cite ADRs by number; that is where to resolve one.

The [test suite's own README](../tests/README.md) explains what is tested and how to run
a subset of it.

## Status

Milestone-01 is in progress. The CLI submits a run and follows it, and the in-cluster
orchestrator parses the definition and plans the run against the cluster's real nodes —
but preflight, execution, measurement, validation, results and cleanup are still being
built, so a submitted run currently exits `5` rather than producing a verdict. [Usage](usage.md#what-a-run-does-today)
says exactly how far a run gets.
