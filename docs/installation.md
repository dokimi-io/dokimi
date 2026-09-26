# Installation

The CLI runs on your machine or in CI. The orchestrator runs in the cluster as a one-shot
Job, and you never install that — the CLI submits it.

## What you need

| | |
| --- | --- |
| Python | **3.14 or newer** |
| Kubernetes | **1.35 or newer**, with credentials that can create Jobs and ConfigMaps in the run's namespace — and, once, an administrator to apply `deploy/rbac.yaml`, which creates that namespace and the `dokimi` lock namespace |
| A container registry | only if your cluster cannot reach `ghcr.io` |

Robot Framework is **not** required to install or run the CLI. It is a runtime dependency
of a run's execution phase, which happens inside the cluster, in an image that has it
(ADR-01-02, ADR-01-08).

## Supported platforms

| Platform | Status |
| --- | --- |
| **macOS**, Apple silicon | supported, tested |
| **Linux**, x86_64 | supported, tested |
| macOS, Intel · Linux, arm64 | supported, **not yet tested** — the tooling handles both architectures, but nobody has run it there |
| **Windows**, through WSL2 | supported, **not yet tested** — WSL2 is Linux, so it takes the Linux path; see below |
| Windows, natively | **not supported** — the test environment is bash scripts, and nothing is tested there |

### Where each part was tested

Last verified on 2026-09-25.

| | macOS 26.6.2, Apple silicon | Linux x86_64 (GitHub Actions `ubuntu-latest`) |
| --- | --- | --- |
| Library and CLI: lint, types, test suite | the full suite, with and without Robot Framework | on every pull request, with and without Robot Framework |
| Orchestrator image | built and run, arm64 | built and run on every pull request, amd64 |
| Against a real cluster | validate, dry-run, submit, attach and planning, on kind with Kubernetes v1.37 | — (the acceptance run, M1-17) |
| [Test environment](../test-env/README.md) | all seven profiles provisioned, checked and torn down; the two default profiles provisioned a second time | the same, whenever `test-env/` changes and weekly |

Both use Python 3.14.0 through `uv` 0.12.18. On macOS the container runtime was OrbStack 2.2.3
(Docker 29.4.0); on Linux, the Docker engine GitHub's runners ship.

**Not tested with:** Docker Desktop, colima or Podman on either platform. Docker Desktop and
colima provide the same Docker API and are expected to work; Podman is not supported by k3d and
only experimentally by kind.

### Windows, through WSL2

WSL2 runs a real Linux kernel, so this is the Linux path rather than a separate port. The steps
below are the standard ones, and not yet tested for this project:

1. Install a Linux distribution in WSL2 — Ubuntu is the usual choice.
2. Make Docker available inside it: either Docker Desktop with its WSL2 integration enabled for
   that distribution, or the Docker engine installed in the distribution itself.
3. **Clone the repository inside the Linux filesystem** (`~/…`), not under `/mnt/c/…`. Under
   `/mnt/c` the files take Windows semantics: executable bits are lost and filesystem access is
   much slower.
4. From there, follow this guide and [Setup](setup.md) as on Linux.

The repository's `.gitattributes` keeps shell scripts and `versions.env` at Unix line endings
even on a checkout with `core.autocrlf=true`, which would otherwise break them.

## Installing

The project is not published to PyPI. Install it from source, which is also what
[Setup](setup.md) does for development:

```console
$ git clone git@github.com:Test-Orchestrator/test-orchestrator.git
$ cd test-orchestrator
$ uv sync --locked
$ uv run dokimi --version
```

To get a `dokimi` command on your `PATH` rather than one behind `uv run`:

```console
$ uv tool install .
```

Once releases are cut, a release will carry a built wheel you can install directly:

```console
$ uv tool install ./dokimi-<version>-py3-none-any.whl
```

[Building](building.md) covers producing that wheel yourself.

## Installing `uv`

`uv` manages the Python version, the virtual environment and the locked dependency set,
so it is the one tool you need before anything else. The project requires **0.12.18 or
newer** — enforced, not just documented, because that release fixes a path-traversal
weakness during wheel installation (GHSA-2cv4-cqwr-gwf7).

```console
$ curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS, Linux
$ brew install uv                                    # or, with Homebrew
```

You do not need to install Python 3.14 separately. `uv sync` reads `.python-version` and
fetches a matching interpreter if your system has none.

## Cluster credentials

The CLI picks cluster credentials in this order:

1. `--kubeconfig` and `--context`, when either is given — and then only those;
2. otherwise, in-cluster service account credentials, when running inside a pod;
3. otherwise, `$KUBECONFIG`, or `~/.kube/config`.

The second entry is why the same command works unchanged in a CI job that runs inside the
cluster it is testing. The first is how to override it there.

Submitting needs to create Jobs and ConfigMaps in the run's namespace, and nothing more:
**a run never creates a namespace** (ADR-05-13). An administrator creates the namespaces
once, and after that submitting needs only namespaced permissions, not the cluster-wide
right to create namespaces. Submitting to a namespace that does not exist fails with a
message saying so.

The Job a run submits authenticates as a service account of its own, not as you. Its
grant is cluster-wide in two places, which is why an administrator applies it once per
cluster:

- it reads the cluster's **nodes** to plan (phase 3);
- it **lists ConfigMaps in every namespace** to check that no other run is active, and
  takes a `Lease` in the `dokimi` namespace while it claims the cluster (phase 4,
  ADR-05-10). Kubernetes cannot restrict a `list` by label, so this grant can read every
  ConfigMap in the cluster, including its data. It is the price of cluster-wide
  exclusivity: one run at a time per cluster.

`deploy/rbac.yaml` creates both namespaces (`dokimi-iperf2` for the runs, `dokimi` for the
run-start lock), the service account, and exactly those grants, each commented with the
phase that needs it:

```console
$ kubectl apply -f deploy/rbac.yaml
```

Runs then pass `--service-account dokimi`. See
[before the first run](usage.md#before-the-first-run).

There is deliberately no configuration file of our own in that chain. The
configuration-sources decision (ADR-04-02) is still open, and Milestone-01 must not
implement a precedence rule a later decision would have to overturn.

## The orchestrator image

A run is a reference to a container image (ADR-01-06), and a released CLI ships with the
digest of the image built from its own commit (ADR-05-08).

**No release has been cut yet, so a source checkout has no default image.** `submit` will
tell you so rather than falling back to a tag that might mean something else tomorrow:

```console
$ dokimi submit test.yaml
dokimi: this is a development build with no published orchestrator image, so
there is no default to submit. Pass an explicit reference or set DOKIMI_IMAGE.
```

Until then — and afterwards, in an air-gapped or policy-constrained cluster — point it at
an image yourself:

```console
$ export DOKIMI_IMAGE=registry.internal/orchestrator@sha256:...
$ dokimi submit test.yaml
```

[Building](building.md#the-orchestrator-image) covers building that image, and
[Usage](usage.md#choosing-the-orchestrator-image) covers the precedence between the flag,
the environment variable and the release default.

## Checking the installation

```console
$ dokimi --version
dokimi 0.1.0

$ dokimi validate examples/worker-01-to-worker-02-udp.yaml
examples/worker-01-to-worker-02-udp.yaml is valid.
...
```

`validate` contacts nothing, so it works before you have a cluster.
