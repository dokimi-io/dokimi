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
$ uv run dokimi --help
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

On Windows, do all of this inside WSL2 — see
[supported platforms](installation.md#windows-through-wsl2).

Any Kubernetes 1.35+ cluster works. For a local one, use the repository's own test environment
in [`test-env/`](../test-env/README.md). It provisions one control plane and three workers,
checks the cluster provides what a run needs, and tears it down again — identically on your
machine and in CI:

```console
$ test-env/install-tools.sh .test-env/bin          # pinned kind, k3d, kubectl, cilium
$ export PATH="$PWD/.test-env/bin:$PATH"
$ test-env/test-env.sh up kind-kindnet
$ test-env/test-env.sh check
$ export KUBECONFIG=.test-env/kubeconfig
```

It never touches your default kubeconfig or switches your current context; the cluster has
its own kubeconfig in `.test-env/`. `kind-kindnet` is the fastest profile. Six others pair kind
or k3d with Calico, Cilium or flannel, for when CNI-specific behaviour matters. The
[environment's README](../test-env/README.md) lists them all, with what each one verifiably
provides.

The cluster needs the orchestrator image, and a local build is not in any registry it can
reach — so build it and load it in directly:

```console
$ docker build -f images/orchestrator/Dockerfile -t orchestrator:dev .
$ test-env/test-env.sh load orchestrator:dev
$ export DOKIMI_IMAGE=orchestrator:dev
```

Grant the run's Job what it needs to plan, once:

```console
$ kubectl apply -f deploy/rbac.yaml
```

The example names `worker-01` and `worker-02`, which this cluster does not have.
`test-env.sh nodes` prints the names it does have, so point a copy of the example at two of
them:

```console
$ test-env/test-env.sh nodes
dokimi-worker
dokimi-worker2
dokimi-worker3
$ sed -e 's/worker-01/dokimi-worker/' -e 's/worker-02/dokimi-worker2/' \
    examples/worker-01-to-worker-02-udp.yaml > kind-pair.yaml
```

Check the plan without creating anything, then submit:

```console
$ uv run dokimi submit kind-pair.yaml --dry-run --kubeconfig .test-env/kubeconfig > /dev/null
$ uv run dokimi submit kind-pair.yaml --service-account dokimi
```

The first command resolves both nodes read-only and prints the plan. The second runs it:
phases 2 and 3 in the cluster, then an exit `5` because phases 4 to 11 are still being
built — see [what a run does today](usage.md#what-a-run-does-today).

Worth trying once, to see the errors a first run can hit: point the definition at
`dokimi-control-plane` (tainted), `kubectl cordon` a worker, or leave off
`--service-account`. Each fails with a message that names its fix.

**What this cluster can and cannot tell you.** Its nodes share one machine's network stack, so
traffic between them is bounded by host CPU and memory bandwidth rather than by a NIC or a real
CNI datapath. It validates **orchestration correctness**, not **measurement realism**:
throughput, loss and jitter measured here say nothing about real hardware, and a threshold
that means something on real hardware means nothing here. That boundary is a recorded decision,
[ADR-05-11](https://github.com/Test-Orchestrator/test-orchestrator-adrs/blob/main/adrs/05-11-emulated-test-environment-validates-orchestration-not-measurement.md),
not a caveat.

Tearing down — and proving nothing was left behind:

```console
$ test-env/test-env.sh down
```

## Repository layout

```text
src/dokimi/
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
deploy/             what an administrator applies once per cluster (RBAC)
test-env/           the multi-node test environment: kind and k3d profiles, check, CI
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
