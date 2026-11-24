# Documentation

Four guides, in the order you are likely to need them, and the test environment's own README.

| | |
| --- | --- |
| [Installation](installation.md) | Getting the CLI onto a machine, and what it needs |
| [Usage](usage.md) | Every command, every flag, and what the exit codes mean |
| [Setup](setup.md) | A development environment, including a throwaway cluster |
| [Test environment](../test-env/README.md) | The multi-node cluster: its profiles, what `check` verifies, and what its numbers do not mean |
| [Building](building.md) | The distribution, the container image, and the release |

The [repository README](../README.md), outside this folder, is the one to read first if you
want to know *why* rather than *how*: the execution model, the phases of a run, the metric
contract and the interfaces. These guides give the reason for a behaviour where they describe
it.

The [test suite's own README](../tests/README.md) explains what is tested and how to run
a subset of it.

## Status

Milestone-01 is in progress. A run goes from submission to its verdict: it checks the
cluster, runs the test, measures it, evaluates the pass criteria, records where everything
ran, writes its results as CSV and JSONL, cleans up, and exits with `0`, `1` or `2`.
[Usage](usage.md#what-a-run-does-today) says exactly what a run does.
