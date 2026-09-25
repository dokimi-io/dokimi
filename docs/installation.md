# Installation

The CLI runs on your machine or in CI. The orchestrator runs in the cluster as a one-shot
Job, and you never install that — the CLI submits it.

## What you need

| | |
| --- | --- |
| Python | **3.14 or newer** |
| Kubernetes | **1.35 or newer**, with credentials that can create Jobs and ConfigMaps in the run's namespace — and, once, an administrator to apply `deploy/rbac.yaml`, which also creates that namespace |
| A container registry | only if your cluster cannot reach `ghcr.io` |

Robot Framework is **not** required to install or run the CLI. It is a runtime dependency
of a run's execution phase, which happens inside the cluster, in an image that has it
(ADR-01-02, ADR-01-08).

## Installing

The project is not published to PyPI. Install it from source, which is also what
[Setup](setup.md) does for development:

```console
$ git clone git@github.com:Test-Orchestrator/test-orchestrator.git
$ cd test-orchestrator
$ uv sync --locked
$ uv run test-orchestrator --version
```

To get a `test-orchestrator` command on your `PATH` rather than one behind `uv run`:

```console
$ uv tool install .
```

Once releases are cut, a release will carry a built wheel you can install directly:

```console
$ uv tool install ./test_orchestrator-<version>-py3-none-any.whl
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

Submitting needs to create Jobs and ConfigMaps in the run's namespace. It checks that the
namespace exists before anything else and creates it only if it does not, so once an
administrator has created it — or a first run has — later submissions need only those
namespaced permissions, not the cluster-wide right to create namespaces.

The Job a run submits authenticates as a service account of its own, not as you. It
needs to read the cluster's nodes to plan, which is a cluster-wide permission, so an
administrator grants it once per cluster:

```console
$ kubectl apply -f deploy/rbac.yaml
```

Runs then pass `--service-account test-orchestrator`. See
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
$ test-orchestrator submit test.yaml
test-orchestrator: this is a development build with no published orchestrator image, so
there is no default to submit. Pass an explicit reference or set TEST_ORCHESTRATOR_IMAGE.
```

Until then — and afterwards, in an air-gapped or policy-constrained cluster — point it at
an image yourself:

```console
$ export TEST_ORCHESTRATOR_IMAGE=registry.internal/orchestrator@sha256:...
$ test-orchestrator submit test.yaml
```

[Building](building.md#the-orchestrator-image) covers building that image, and
[Usage](usage.md#choosing-the-orchestrator-image) covers the precedence between the flag,
the environment variable and the release default.

## Checking the installation

```console
$ test-orchestrator --version
test-orchestrator 0.1.0

$ test-orchestrator validate examples/worker-01-to-worker-02-udp.yaml
examples/worker-01-to-worker-02-udp.yaml is valid.
...
```

`validate` contacts nothing, so it works before you have a cluster.
