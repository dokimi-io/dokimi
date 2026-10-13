# The test suite

670 tests, none of which need a cluster, a network or a container runtime. Everything runs
in about a second.

```console
$ uv run pytest                                 # all of it
$ uv run pytest tests/test_submission.py        # one file
$ uv run pytest -k reattach                     # by name
$ uv run pytest -x -vv                          # stop at the first failure, verbosely
```

`-q` is on by default via `pyproject.toml`, so pass `-v` when you want the test names.

## What is tested where

| File | Tests | Subject |
| --- | --- | --- |
| [`test_model.py`](test_model.py) | 51 | The canonical model: durations, bandwidths, criteria, and what it refuses — including every value a renderer could not carry |
| [`test_formats.py`](test_formats.py) | 23 | The YAML and Robot plugins, and that they are genuinely peers |
| [`test_robot_strictness.py`](test_robot_strictness.py) | 31 | The Robot parser reads every criterion Robot would call, or rejects the suite |
| [`test_examples.py`](test_examples.py) | 8 | The shipped examples parse and agree; every test definition in the docs parses, and the README shows the examples verbatim |
| [`test_planning.py`](test_planning.py) | 24 | Discovery, explicit node resolution, and the plan |
| [`test_identity.py`](test_identity.py) | 19 | Run ids and ownership labels |
| [`test_run_identity.py`](test_run_identity.py) | 18 | The `RunId` type ownership and exclusivity are written against |
| [`test_ownership.py`](test_ownership.py) | 19 | Run-owned object creation, owner references, and label-scoped deletion |
| [`test_exclusivity.py`](test_exclusivity.py) | 66 | One run at a time: the run-start lease, the claim lifecycle and its expiry, and two racing submissions through `submit` |
| [`test_deadlines.py`](test_deadlines.py) | 14 | Workload and Job deadline budgets, including warm-up, and the Pod and Job bounds they enforce |
| [`test_namespaces.py`](test_namespaces.py) | 7 | Per-test-type namespace naming |
| [`test_testtype_interface.py`](test_testtype_interface.py) | 14 | The test-type interface: parameter schemas, capabilities, workload images, and the plugin registry |
| [`test_iperf2.py`](test_iperf2.py) | 42 | The `iperf2` plugin: capabilities per protocol, the warm-up pass, its pods, start, completion, and both role scripts' contracts |
| [`test_lifecycle.py`](test_lifecycle.py) | 24 | Deploying a pair, the per-test readiness gate and its error categories, IPv4 addressing, and that `start()` is never called before the gate passes |
| [`test_prometheus.py`](test_prometheus.py) | 7 | The Prometheus client, and phase 4's query-access check |
| [`test_managed_prometheus.py`](test_managed_prometheus.py) | 44 | The managed Prometheus: mode selection, what it scrapes, supporting exporters, the node-exporter it deploys when there is none, staying off the tested nodes, and that it is kept unless removal is asked for |
| [`test_execution.py`](test_execution.py) | 27 | Phases 5, 6 and 9a with a fake runner, and what `Run Network Test` does against an in-memory API server |
| [`test_robot_execution.py`](test_robot_execution.py) | 4 | What Robot Framework does with the rendered suite: YAML and Robot run identically, every criterion says it was not evaluated, and a real `robot` and `rebot` produce `log.html`. Skipped without Robot Framework |
| [`test_artifact_stream.py`](test_artifact_stream.py) | 13 | A run's artifacts through the Job's log and back: exact round trip, never printed, refused when altered, truncated or reaching outside its directory |
| [`test_manifests.py`](test_manifests.py) | 19 | What a submission renders, and the properties those manifests must have |
| [`test_submission.py`](test_submission.py) | 33 | Submitting, following and reattaching, and which exit codes are a verdict |
| [`test_cluster_adapter.py`](test_cluster_adapter.py) | 6 | How the real Kubernetes adapter translates the client library's failures |
| [`test_kubernetes_adapter.py`](test_kubernetes_adapter.py) | 13 | The per-kind resource adapter builds real client methods and maps HTTP status to errors |
| [`test_cli.py`](test_cli.py) | 71 | Every command, and the exit code each produces |
| [`test_images.py`](test_images.py) | 15 | Orchestrator image resolution and override precedence |
| [`test_run_images.py`](test_run_images.py) | 22 | The images a run deploys: chosen at submission, by run, installation or release default, and carried into the Job |
| [`test_image_build.py`](test_image_build.py) | 21 | Both Dockerfiles and the release wiring keep their promises |
| [`test_test_env.py`](test_test_env.py) | 27 | The test environment's configs, scripts, README and CI workflow agree with each other |
| [`test_workflows.py`](test_workflows.py) | 4 | Every GitHub Action the workflows use is pinned to a commit |
| [`test_rbac.py`](test_rbac.py) | 12 | `deploy/rbac.yaml` grants what the Job needs, and stays in step with the code |
| [`test_package_layout.py`](test_package_layout.py) | 26 | The prescribed package layout exists and imports |
| [`test_core_independence.py`](test_core_independence.py) | 1 | The core library does not pull in Robot Framework |

## How the cluster-facing tests work without a cluster

Everything the CLI does to a cluster goes through a small protocol, `ClusterClient`. [`fake_cluster.py`](fake_cluster.py) provides two implementations:

- **`FakeCluster`** records every write and answers reads from a script — a list of pod
  phases, a log, an exit code, the cluster's nodes. Assertions are then made about the calls: that the owner
  object was created first, that the pod was located by label selector and not by name,
  that the container's exit code was propagated unchanged.
- **`ExplodingCluster`** raises on contact. Any code path that must not reach a cluster
  gets one of these, and the test passing *is* the assertion. It is what `--dry-run` and
  "invalid input never reaches the cluster" are tested with, and it is a stronger claim
  than a comment saying so.

`node()` builds a Node object in the API's shape — `node("worker-02", cordoned=True)`,
`node("cp", taints=[("node-role.kubernetes.io/control-plane", "", "NoSchedule")])` — with
only the fields discovery reads. Planning itself needs no fake at all: it is a pure function
of a definition and a node snapshot, so `test_planning.py` builds the snapshot directly.

The one thing the fakes cannot reach is the real adapter, `KubernetesCluster`, and how it
translates the client library's failures — an `ApiException`, or a urllib3 error when the
API server cannot be reached at all. [`test_cluster_adapter.py`](test_cluster_adapter.py)
covers that by handing a real `KubernetesCluster` a stand-in for the library's `CoreV1Api`
that raises whatever the test asks for.

Ownership, exclusivity and cleanup use a second, per-kind protocol, `ResourceClient` in
`k8s/resources.py`: create, get, list, replace, and delete with uid and resourceVersion
preconditions. [`fake_resources.py`](fake_resources.py)'s **`FakeResources`** is an
in-memory API server with the semantics those rules depend on — an atomic `create` that
fails on a taken name, preconditions that are honoured, label selectors evaluated
server-side, all behind one lock so concurrent callers race the way real ones do.
`FakeCluster` stores everything it creates in a `FakeResources` too, and hands it out from
`resources()`, so a test can submit through M1-04's path and claim through M1-05's
against one cluster.
[`test_kubernetes_adapter.py`](test_kubernetes_adapter.py) checks the real adapter,
`KubernetesResources`, against the client library's actual method names.

Time is injected too: `follow()` takes `sleep` and `clock`, so a test can exercise the
"pod is still Pending" and "the log stream ended before the status caught up" paths
without waiting for either.

## Tests that are about decisions, not behaviour

Seven files assert things that would otherwise decay quietly, because the code keeps
working while being wrong:

- [`test_image_build.py`](test_image_build.py) reads the Dockerfile as text and checks the
  base image, the lockfile-only install and the non-root user. CI catches a build that
  breaks; only this catches a build that still works while having changed its mind. It
  also checks that the regexes the release workflow rewrites still match the generated
  digest file, and that every digest in it is rewritten — reformatting that file, or adding
  an image the release forgets, would otherwise break a release without breaking anything a
  test run would notice. The iperf2 image gets the same treatment: Alpine pinned by digest,
  the `iperf` package pinned by version, non-root, and both its role scripts in the build
  context. Both images are labelled with the project's MIT license.
- [`test_robot_strictness.py`](test_robot_strictness.py) guards against a false PASS. The
  suite that executes is re-rendered from the parsed model, so a criterion the parser
  skips is one that is never checked — and the run passes. Every way that was possible
  when #26 was reviewed (a lowercased keyword, a typo, a criterion under `IF` or `ELSE`,
  a `[Teardown]`, a shadowing `*** Keywords ***`) is a case here, in both directions: what
  Robot would call must be read, and what the renderer would not emit must be refused.
- [`test_manifests.py`](test_manifests.py) asserts `backoffLimit: 0` and
  `restartPolicy: Never`. A Job with the defaults runs perfectly well and silently re-runs
  the entire matrix when a run exits non-zero for a plain failure.
- [`test_rbac.py`](test_rbac.py) parses `deploy/rbac.yaml` and checks it against the code:
  the namespaces runs actually use, the service account the error message tells people to
  pass, and the exact grant, rule by rule, so that each widening is a deliberate change. An administrator applies that file by hand and the
  code never reads it, so nothing else would notice the two drifting apart until a real
  cluster refused a call.
- [`test_test_env.py`](test_test_env.py) holds the [test environment](../test-env/README.md) to its
  own promises without provisioning anything:
  - every profile is one control plane and three workers;
  - every version lives in `versions.env`, with images pinned by digest and manifests by checksum;
  - the script, the CI workflow and the README list the same profiles, with the same measured
    NetworkPolicy enforcement;
  - the scripts avoid bash-4-only syntax, because macOS still ships bash 3.2;
  - `up` records the profile before it creates a cluster, so a failed `up` never leaves one
    that `down` refuses to remove;
  - the kernel settings `up` announces before creating a cluster are the ones in the README's
    measured table.

  CI's test-environment workflow does the provisioning. This is what catches the one-line edit
  that breaks it before anyone does.
- [`test_workflows.py`](test_workflows.py) checks that every action in `.github/workflows/`
  is pinned to a 40-character commit SHA, with the version in a trailing comment. A tag such as
  `@v4` can be moved to different code by whoever controls that repository, and the release
  workflow runs with `packages: write` and `id-token: write`. A tag works just as well until
  the day it is moved, so only a test notices it coming back.
- [`test_core_independence.py`](test_core_independence.py) imports every core module in a
  subprocess and fails if anything named `robot` appears in `sys.modules`. The subprocess
  is the point: the result must not be contaminated by whatever the rest of the session
  already imported.

## The `requires_robot` marker

The Robot format plugin needs Robot Framework, and one CI job exists specifically to run
without it. Tests that exercise that plugin are marked:

```python
@pytest.mark.requires_robot
def test_a_robot_suite_round_trips() -> None:
    assert parse_robot(render_robot(definition)) == definition
```

[`conftest.py`](conftest.py) skips them when the distribution is absent, so the job that
proves the core works without Robot is not failed by the tests that cannot run there.

To see it from both sides:

```console
$ uv sync --locked --all-extras && uv run pytest   # everything
$ uv sync --locked && uv run pytest                # the marked tests skip
```

Note that `conftest.py` asks `importlib.metadata` whether the *distribution* is installed,
rather than trying to import `robot`. This repository has a `robot/` directory at its root
which Python treats as a namespace package, so that import succeeds even when Robot
Framework is not installed, and would prove nothing.

## Writing a test here

A few conventions the existing tests follow:

- **Name the property, not the function.** `test_following_finds_the_pod_by_label_not_by_name`
  says what is at stake; `test_follow` does not.
- **Say why in a comment when the reason is a decision.** Most tests here are guarding an
  ADR, and the comment is where a future reader learns which one and what it protects
  against. Do not comment what the code plainly says.
- **Prefer a fake over a mock.** The fakes record calls and answer from a script; nobody
  has to assert on call signatures.
- **Test classes are not collected.** `pyproject.toml` sets `python_classes = []`, because
  the model has classes named `TestDefinition` and `TestType` that pytest would otherwise
  try to collect. Every test here is a function.
- **Keep it offline.** A test that needs a cluster, a registry or a network belongs
  somewhere else; the acceptance run against a real cluster is M1-17 (#52).
