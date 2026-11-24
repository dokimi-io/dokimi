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
of a run's execution phase, which happens inside the cluster, in an image that has it.

## Supported platforms

| Platform | Status |
| --- | --- |
| **macOS**, Apple silicon | supported, tested |
| **Linux**, x86_64 | supported, tested |
| macOS, Intel · Linux, arm64 | supported, **not yet tested** — the tooling handles both architectures, but nobody has run it there |
| **Windows**, through WSL2 | supported, tested — WSL2 is Linux, so it takes the Linux path; see below |
| Windows, natively | **not supported** — the test environment is bash scripts, and nothing is tested there |

### Where each part was tested

The WSL2 column was verified on 2026-11-12; the macOS and Linux columns were last verified on 2026-09-25.

| | macOS 26.6.2, Apple silicon | Linux x86_64 (GitHub Actions `ubuntu-24.04`) | Windows 11 through WSL2, x86_64 |
| --- | --- | --- | --- |
| Library and CLI: lint, types, test suite | the full suite, with and without Robot Framework | on every pull request, with and without Robot Framework | the full suite, with and without Robot Framework |
| Orchestrator image | built and run, arm64 | built and run on every pull request, amd64 | built and run, amd64, with the `iperf2` image's own CI checks |
| Against a real cluster | validate, dry-run, submit, attach and planning, on kind with Kubernetes v1.37 | — (the acceptance run, M1-17) | runs to a verdict on kind and k3d with Kubernetes v1.37: a pass, a failed criterion and an error, each with the exit code and the evidence [Usage](usage.md#what-a-run-does-today) describes; `preflight`, ready and with an image that cannot be pulled; one run at a time; `cleanup`; the three first-run errors [Setup](setup.md#a-cluster-to-submit-runs-against) lists; shell completion in bash, zsh and fish |
| [Test environment](../test-env/README.md) | all seven profiles provisioned, checked and torn down; the two default profiles provisioned a second time | the same, whenever `test-env/` changes and weekly | all seven profiles provisioned, checked and torn down; the two default profiles provisioned a second time |

macOS and Linux use Python 3.14.0 through `uv` 0.12.18. On macOS the container runtime was
OrbStack 2.2.3 (Docker 29.4.0); on Linux, the Docker engine GitHub's runners ship. WSL2 was
WSL 3.0.5 with kernel 6.18.58.1 and Ubuntu 22.04, in mirrored networking mode, using Python
3.14.7 through `uv` 0.12.18 and the Docker engine 28.3.2 installed in the distribution rather
than Docker Desktop.

**Not tested with:** Docker Desktop, colima or Podman on any platform, including Docker
Desktop's WSL2 integration. Docker Desktop and colima provide the same Docker API and are
expected to work; Podman is not supported by k3d and only experimentally by kind.

### Windows, through WSL2

WSL2 runs a real Linux kernel, so this is the Linux path rather than a separate port. These are
the steps the WSL2 column above was verified with:

1. Install a Linux distribution in WSL2 — Ubuntu is the usual choice.
2. Make Docker available inside it: either the Docker engine installed in the distribution
   itself, which is what was tested, or Docker Desktop with its WSL2 integration enabled for that
   distribution, which is expected to work but was not tested.
3. **Clone the repository inside the Linux filesystem** (`~/…`), not under `/mnt/c/…`. Under
   `/mnt/c` the files take Windows semantics: every file shows as mode `0777`, so the scripts
   still run but their real permissions are lost, and filesystem access is much slower.
4. From there, follow this guide and [Setup](setup.md) as on Linux.

The repository's `.gitattributes` keeps shell scripts and `versions.env` at Unix line endings
even on a checkout with `core.autocrlf=true`, which would otherwise break them. That was checked
on such a checkout: most other files get CRLF, including `pyproject.toml`, `uv.lock` and the
cluster configs, and `uv sync --locked`, the test suite and `ruff format --check` still pass.
Provisioning a cluster from those CRLF cluster configs was not tried.

What differs from a Linux machine is all about the kernel the test environment's nodes share:

- **The kernel is the WSL2 VM's, shared by every distribution you run.** Where the
  [test environment](../test-env/README.md#what-it-changes-on-your-machine) says a module or
  setting stays "until reboot", on WSL2 that means until `wsl --shutdown`, which resets all of
  them. An oops that `kernel.panic=10` turns into a panic restarts the WSL2 VM and every
  distribution in it, not Windows.
- **`kernel.core_pattern` starts out as WSL's own crash reporter** (`|/wsl-capture-crash …`).
  The kind profiles set it to `core`, which switches WSL's crash capture off until
  `wsl --shutdown`.
- **`kernel.panic_on_oops` is already `1`**, so `up` does not report it as changed.
- **`br_netfilter` was already loaded** on the tested machine, so `kind-flannel` came up without
  `--load-modules` and without `sudo`. `up` checks for it either way.

One difference is in WSL itself rather than the kernel. In mirrored networking mode, WSL 2.7.14
accepted a connection to a closed port on `127.0.0.1` instead of refusing it, so
`test_the_tcp_trigger_gives_up_after_its_attempts` in `tests/test_iperf2.py` fails there. WSL
3.0.3 and 3.0.5 refuse it, and the test passes. The connection dokimi itself makes goes to a pod
IP inside the cluster, which it reached on every version tried.

## Installing

The project is not published to PyPI. Install it from source, which is also what
[Setup](setup.md) does for development:

```console
$ git clone git@github.com:dokimi-io/dokimi.git
$ cd dokimi
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

These choose the cluster a run goes to. Its **name**, which labels the results, is separate and
has no default: `--cluster <name>` on `submit` and `preflight`, or `DOKIMI_CLUSTER` for an
installation, as in `export DOKIMI_CLUSTER=lab-fra-1` next to the credentials a machine submits
with. See [Naming the cluster](usage.md#naming-the-cluster).

Submitting needs only namespaced permissions in the run's namespace: **a run never creates
a namespace**. An administrator creates the namespaces once, and after that
nobody needs the cluster-wide right to create one. Submitting to a namespace that does not
exist fails with a message saying so. What each command needs in that namespace:

| Command | Needs |
| --- | --- |
| `submit`, `preflight` | `create` on Jobs and ConfigMaps; `get` on Pods and their `log`, and `list` on Jobs and Pods, to follow the run; and, once the reports are fetched, `get` and `delete` on ConfigMaps and `list` and `delete` on Jobs, to remove the run |
| `attach` | the same, without `create`; `get` on ConfigMaps also finds the run's `results.directory` |
| `cleanup` | `get` on ConfigMaps, and `list` and `delete` on ConfigMaps, Pods, Services, ServiceAccounts, Jobs, Deployments, DaemonSets and StatefulSets |
| `prometheus` | `list` on Deployments and Pods and `get` on ConfigMaps, to find the run's Prometheus, and `create` on `pods/portforward`, to reach it |

A missing `delete` after a fetch is only a warning: the cluster removes the Job an hour
after the run ended, and `dokimi cleanup` removes the rest.

The Job a run submits authenticates as a service account of its own, not as you. Its
grant is cluster-wide in two places, which is why an administrator applies it once per
cluster:

- it reads the cluster's **nodes** to plan (phase 3);
- it **lists ConfigMaps in every namespace** to check that no other run is active, and
  takes a `Lease` in the `dokimi` namespace while it claims the cluster (phase 4).
  Kubernetes cannot restrict a `list` by label, so this grant can read every ConfigMap in
  the cluster, including its data. It is the price of cluster-wide exclusivity: one run at
  a time per cluster.

`deploy/rbac.yaml` creates both namespaces (`dokimi-iperf2` for the runs, `dokimi` for the
run-start lock), the service account, and exactly those grants, each commented with the
phase that needs it:

```console
$ kubectl apply -f deploy/rbac.yaml
```

Runs then pass `--service-account dokimi`. See
[before the first run](usage.md#before-the-first-run).

There is deliberately no configuration file of our own in that chain. The
configuration-sources decision is still open, and Milestone-01 must not
implement a precedence rule a later decision would have to overturn.

## The orchestrator image

A run is a reference to a container image, and a released CLI ships with the
digest of the image built from its own commit.

**No release has been cut yet, so a source checkout has no default image.** `submit` will
tell you so rather than falling back to a tag that might mean something else tomorrow:

```console
$ dokimi submit test.yaml
dokimi: this is a development build with no published orchestrator image, so there is no
default to run. Choose one with --image orchestrator=<reference> or DOKIMI_ORCHESTRATOR_IMAGE
```

Until then — and afterwards, in an air-gapped or policy-constrained cluster — point it at
an image yourself:

```console
$ export DOKIMI_ORCHESTRATOR_IMAGE=registry.internal/orchestrator@sha256:...
$ dokimi submit test.yaml
```

`DOKIMI_IMAGE`, its earlier name, still works.

[Building](building.md#the-orchestrator-image) covers building that image, and
[Usage](usage.md#choosing-the-orchestrator-image) covers the precedence between the flag,
the environment variable and the release default.

The same holds for the images a run deploys, today the `iperf2` test workload: a source
checkout has none, and `submit` refuses until one is chosen with `--image iperf2=<reference>`
or `DOKIMI_IPERF2_IMAGE` ([Usage](usage.md#choosing-the-images-a-run-deploys)).

## Checking the installation

```console
$ dokimi --version
dokimi 0.1.0

$ dokimi validate examples/worker-01-to-worker-02-udp.yaml
examples/worker-01-to-worker-02-udp.yaml is valid.
...
```

`validate` contacts nothing, so it works before you have a cluster.

## Shell completion

`dokimi completion bash`, `zsh` or `fish` prints a script that completes dokimi's commands,
flags and files when you press tab. [Usage](usage.md#completion) says how to load it, once or
in every new shell.
