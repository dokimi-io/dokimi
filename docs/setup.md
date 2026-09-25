# Setup

A development environment, and a throwaway cluster to submit runs against.

## The environment

```console
$ git clone git@github.com:Test-Orchestrator/test-orchestrator.git
$ cd test-orchestrator
$ uv sync --locked --all-extras
```

That is the whole setup. `uv` reads `.python-version`, fetches Python 3.14 if your system
has none, creates `.venv/` and installs the exact dependency set recorded in `uv.lock`.

`--locked` fails rather than re-resolving if `uv.lock` does not match `pyproject.toml`. It
is the flag to use everywhere, including CI and the container build, because it is what
makes "the same lockfile commit" mean the same dependency set.

`--all-extras` adds Robot Framework. The core library must work without it (ADR-01-02), so
if you want an environment that proves that, sync without extras instead:

```console
$ uv sync --locked          # no Robot Framework; the suite still passes
```

Run anything through `uv run`, which uses the project environment whether or not you have
activated it:

```console
$ uv run test-orchestrator --help
$ uv run pytest
```

## The checks CI runs

Four commands. Run them before pushing and CI holds no surprises:

```console
$ uv run ruff check .          # lint
$ uv run ruff format --check . # formatting (drop --check to fix)
$ uv run mypy                  # types, strict mode
$ uv run pytest                # tests
```

CI runs these as three jobs plus a fourth that builds the container image and checks it
runs as non-root with Robot Framework present. [Building](building.md) covers that image.

Nothing here needs a cluster. The suite exercises the submission path against an in-memory
fake — see the [test suite README](../tests/README.md).

## Adding a dependency

```console
$ uv add somepackage             # runtime
$ uv add --dev somepackage       # development only
$ uv add --optional robot ...    # into the robot extra
```

Each of these updates `pyproject.toml` and `uv.lock` together. Commit both: editing
`pyproject.toml` by hand and not re-locking makes `uv sync --locked` fail in CI and in the
image build, which is the mechanism working as intended rather than a nuisance.

Dependency floors in this project are the newest release at the time the dependency was
added, and `uv.lock` pins what we actually ship. The bound in `pyproject.toml` governs
what a consumer of the library may resolve.

## A cluster to submit runs against

Any Kubernetes 1.35+ cluster works. For a local one, [kind](https://kind.sigs.k8s.io) is
the least ceremony:

```console
$ kind create cluster --name test-orchestrator
```

The cluster needs the orchestrator image, and a local build is not in any registry it can
reach — so build it and load it in directly:

```console
$ docker build -f images/orchestrator/Dockerfile -t orchestrator:dev .
$ kind load docker-image orchestrator:dev --name test-orchestrator
$ export TEST_ORCHESTRATOR_IMAGE=orchestrator:dev
```

Then submit:

```console
$ uv run test-orchestrator submit examples/worker-01-to-worker-02-udp.yaml
```

The example names `worker-01` and `worker-02`, which a one-node kind cluster does not
have. That does not matter yet — node resolution is M1-06 (#41), and today's run stops
after parsing. It will matter once planning lands, at which point a multi-node cluster is
what you want:

```console
$ kind create cluster --name test-orchestrator --config - <<'YAML'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
YAML
```

kind names those nodes `<cluster>-worker` and `<cluster>-worker2`, not `worker-01`, so
adjust the definition's `placement` to match.

A caution worth stating early: emulated nodes on one machine share that machine's network
stack, so inter-node traffic is bounded by host CPU and memory bandwidth rather than by a
NIC or a real CNI datapath. Such an environment validates **orchestration correctness**,
not **measurement realism**, and any thresholds you set against it are specific to it.

Tearing down:

```console
$ kind delete cluster --name test-orchestrator
```

## Repository layout

```text
src/test_orchestrator/
    model/          canonical test definition model
    formats/        parse() / render() plugins (robot, yaml)
    planning/       node resolution, matrix, concurrency plan
    preflight/      run-level cluster checks
    k8s/            Kubernetes client wrappers, ownership labels, manifests
    testtypes/      test type plugins
    metrics/        exporter read, Prometheus client
    validation/     criteria evaluation
    results/        CSV + JSONL writers
    diagnostics/    post-run snapshot
    cleanup/        run-scoped teardown
    cli/            local CLI entry point
robot/              Robot resources and a thin adapter over the core library
images/             Dockerfiles
exporters/          per-test-type exporters
tests/              the test suite
examples/           example .robot and .yaml definitions
docs/               these guides
```

Several of those packages are documented placeholders today; the docstring in each names
the phase of a run it owns and the ADRs behind it.

The rule that shapes this layout: **the core library has no Robot Framework imports.**
Robot is a runtime dependency of a run's execution phase, not of the library, and the CLI,
a CI entry point and a future Operator are all thin adapters over the same code. The one
exception is the Robot *format plugin*, which imports Robot's parsing API to read a
`.robot` suite — a different role, kept distinct on purpose. `tests/test_core_independence.py`
enforces this rather than leaving it to convention.
