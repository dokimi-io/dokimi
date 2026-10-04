# Usage

The CLI is phase 1 of a run and the only part that executes outside the cluster. It
validates the definition, renders the manifests, submits the Job and streams its logs
back. Everything after that happens in the cluster (ADR-01-06).

```console
$ dokimi validate test.yaml          # parse and check, no cluster involved
$ dokimi convert test.yaml           # print the Robot suite a run executes
$ dokimi submit test.yaml --dry-run  # render the matrix and the manifests
$ dokimi submit test.yaml            # create the run and stream its logs
$ dokimi attach <run-id>             # resume streaming a run already going
```

- [What a run does today](#what-a-run-does-today)
- [Before the first run](#before-the-first-run)
- [validate](#validate)
- [convert](#convert)
- [submit](#submit)
- [attach](#attach)
- [image](#image)
- [Exit codes](#exit-codes)
- [How nodes are resolved](#how-nodes-are-resolved)
- [What a submission creates](#what-a-submission-creates)
- [Choosing the orchestrator image](#choosing-the-orchestrator-image)
- [Choosing the images a run deploys](#choosing-the-images-a-run-deploys)
- [Cleaning up](#cleaning-up)

## What a run does today

Milestone-01 is not finished. A submitted run parses its definition, resolves its two
nodes against the cluster, plans the pair, claims the cluster, and then **runs the test
through Robot Framework**: it renders the suite, deploys the pair's pods, waits until both
are Ready, starts the measurement and waits for it to finish. Then it stops:

```
run 20261002-120000-a1b2c3
phase 2 (ingestion): definition parsed
...
phase 3 (planning): 1 test, sequential
  pair-001  worker-01 -> worker-02
phase 4 (preflight): cluster claimed for this run until 2026-10-02T12:20:30Z
phase 4 (preflight): the cluster checks are not implemented in this build (M1-07, #42); the run continues without them

phase 5 (suite rendering): /work/artifacts/20261002-120000-a1b2c3/suite.robot
phase 6 (execution): pair-001  worker-01 -> worker-02, one Robot Framework process
...
phase 9a (reporting): /work/artifacts/20261002-120000-a1b2c3/log.html, .../report.html

the rest of phases 7 to 11 is not implemented in this build: measurement (#47),
validation (#48), results (#49), cleanup and outcome (#51). The run stops here without a
verdict; Robot's report says which steps ran.
```

It exits `5`, not `0`. A run that stops early must not look like a run that passed. A
node that is missing or cannot take a test pod stops it earlier, in phase 3, with `3`.

Three things to know about a run today:

- **Every pass criterion fails, and says it was not checked.** Evaluating criteria
  arrives with M1-13, so `Throughput Should Be At Least 950 megabits per second` fails with
  "was not checked" rather than passing, or being skipped, without anything compared.
- **Robot's report is written inside the Job**, in `/work/artifacts/<run-id>/`, next to the
  suite that ran. Getting it back to you arrives next (ADR-05-15).
- **The run removes its test pods before it frees the cluster,** however it ended, and
  waits until they are gone: a server pod would otherwise idle until its deadline, and a
  pod still sending must keep the cluster claimed (ADR-05-13). That takes the pods of a
  failed run too, so for now a failure keeps its report but not its pods; what a failed run
  keeps is M1-16's. If the pods cannot be removed, the run says so and keeps its claim until
  it expires. It prints the command that frees it earlier, which selects the test's
  client and server pods only: `kubectl delete pod -n dokimi-iperf2 -l
  'dokimi.io/run-id=<run-id>,app.kubernetes.io/component in (client,server)'`.

Everything else on this page — validation, conversion, dry-run rendering, node
resolution, submission, streaming, reattachment, exit-code propagation — works now.

## Before the first run

The run's Job reads the cluster's nodes to plan and checks, across every namespace, that
no other run is active. Both are cluster-wide, so the namespace's default service account
cannot. Grant it once per cluster:

```console
$ kubectl apply -f deploy/rbac.yaml
```

That creates the `dokimi-iperf2` namespace for runs, the `dokimi` namespace for the
run-start lock, a `dokimi` service account, and its grants: `get` and `list` on nodes,
`list` on ConfigMaps cluster-wide, the run-start lease in `dokimi`, and in
`dokimi-iperf2` `get` and `update` on ConfigMaps, `get` on Jobs, and `create`, `get`,
`list` and `delete` on Pods: each pair's client and server, removed again when the run
ends. Then submit with that service account:

```console
$ dokimi submit test.yaml --service-account dokimi
```

Without it, the run gets as far as phase 3 and stops with the fix spelled out:

```
dokimi: listing nodes was refused. Planning resolves the definition's nodes
against the cluster, which needs `get` and `list` on nodes — a cluster-wide permission.
deploy/rbac.yaml grants exactly that; apply it, then submit with --service-account
dokimi.
```

The grant widens as later phases land, one commented rule per phase.

A test also needs two network paths inside `dokimi-iperf2`. If a NetworkPolicy applies
there, it has to allow both (ADR-05-03):

- **the Job to each client pod on 5101/TCP.** That is how a test is started: the Job
  connects and sends the server's address, instead of running a command inside the pod;
- **the client pod to the server pod on 5001,** over the test's protocol, UDP or TCP.

Test traffic uses the pod network and IPv4, so a pod network without IPv4 cannot run a
test. Both paths and the address family are what a test needs once a run deploys its test
pods. No run does that yet.

The service account exists only in `dokimi-iperf2`, the namespace
`deploy/rbac.yaml` creates. A run never creates a namespace, so one submitted with
`--namespace` somewhere else needs that namespace to exist already, and will not find the
service account there. For another namespace, copy the file, change the namespace in the
run `Namespace`, the `ServiceAccount`, the namespaced `Role` and every binding's subject,
and apply that copy. Leave the `dokimi` lock namespace as it is: there is one lock per
cluster.

## One run at a time

A cluster runs one run at a time, whatever its namespace or test type: two runs would
contend for the same network they measure (ADR-05-02). The Job claims the cluster as the
first step of phase 4. A second run started meanwhile is refused and exits `4`:

```
dokimi: refusing to start run 20260924-201500-p7m2q8 (run_already_active): run 20260924-200229-ca532a already active in this cluster.
  - ConfigMap dokimi-iperf2/run-20260924-200229-ca532a-owner claimed at 2026-09-24T20:02:41Z, claim expires at 2026-09-24T20:25:11Z
Wait for it to finish, or for its claim to expire. If it is stale, clear it by deleting its owner, which removes what it created with it:
  kubectl delete configmap -n dokimi-iperf2 run-20260924-200229-ca532a-owner
```

Only a run that has claimed the cluster counts (ADR-05-13). A run stops counting:

- **when it ends**, however it ends: once its test pods are gone it marks itself finished,
  before any other cleanup, so a failed run whose objects are kept as evidence does not hold
  the cluster;
- **when its claim expires**, if it crashed: the claim expires with the Job's own deadline,
  its start time plus its `activeDeadlineSeconds`, by which time the cluster has stopped the
  Job and everything it ran. A claim with no readable expiry never counts.

A submission that was refused, or whose Job never started, never claimed anything and
blocks nobody. Neither do workloads or a kept Prometheus left behind by earlier runs.

## validate

```console
$ dokimi validate examples/worker-01-to-worker-02-udp.yaml
examples/worker-01-to-worker-02-udp.yaml is valid.

  name        worker-01-to-worker-02-udp
  pair        worker-01 -> worker-02
  test        iperf2 over udp
  duration    30s
  bandwidth   1G
  monitoring  managed
  results     ./results
  criteria    2
    - throughput atLeast 950 megabits per second
    - packetLoss atMost 0 percent
```

Parses the definition into the canonical model and reports what it says. It contacts
nothing, so it works without a cluster or credentials.

Both authoring formats are accepted, chosen by file suffix: `.yaml`/`.yml` and `.robot`.
They are peers, not a primary and a fallback — no criterion is expressible in one and not
the other (ADR-04-05). See the [repository README](../README.md#authoring-a-test) for the
syntax of each.

Both are strict, and for the same reason. What executes is never the file you wrote: it
is a suite rendered from the parsed model. So anything the parser does not understand
would silently not run, and a criterion that silently does not run is a test that passes
without checking it. Rather than risk that, anything unrecognised is refused:

- in YAML, any field the schema does not define;
- in a `.robot` suite, anything the renderer would not itself emit — any keyword other
  than `Run Network Test` and the criterion keywords, control structures such as `IF`
  and `FOR`, `[Setup]`, `[Teardown]`, `Suite Teardown`, other imports, a
  `*** Keywords ***` section, a second test case, or a variable it does not know.

Keyword names are matched the way Robot matches them, so `packet loss should be` and
`Packet_Loss_Should_Be` are both read as `Packet Loss Should Be`. Every refusal names the
line and the reason:

```
dokimi: line 26: unrecognised keyword 'Packet Loss Shuld Be'. A test case may
call only 'Run Network Test' and the criterion keywords, because anything else would not
be carried into the suite that runs.
```

A keyword that does end in `Should Be` but names no metric — `Packet Lost Should Be` —
also gets the list of criterion keywords that do exist.

The name, node names and results directory are checked too, since they are written into
that rendered suite: node names must be valid Kubernetes node names, and the others may
use letters, digits, single spaces and a little punctuation.

A malformed definition is a `config_error` and exits `3`. The summary is worth reading
even when it passes: it shows the *parsed* values, so `1G` coming back as `1G` and not
`1073741824` confirms the decimal-prefix rule was applied as intended.

## convert

Prints a test definition in the other format, and touches nothing:

```console
$ dokimi convert test.yaml             # the Robot suite
$ dokimi convert suite.robot --to yaml # the YAML document
```

`--to robot`, the default, prints exactly the suite a run executes: phase 5 of every run
calls the same function (ADR-01-08), so what you see here is byte for byte what Robot
Framework runs in the cluster.

## submit

```console
$ dokimi submit test.yaml
submitted run 20260924-200229-ca532a to namespace dokimi-iperf2
run 20260924-200229-ca532a
...
```

Validates locally first, so an invalid definition never reaches the cluster. Then it
creates the run's objects and streams the Job's log until it ends, exiting with the run's
own exit code.

| Flag | |
| --- | --- |
| `--dry-run` | render and stop; contacts nothing |
| `--no-follow` | submit and return, rather than streaming |
| `--run-id` | use a specific run id instead of generating one |
| `--namespace`, `-n` | override the test type's namespace |
| `--image <component>=<reference>` | choose an image for this run, repeatable: `orchestrator`, the Job's own, or `iperf2` |
| `--orchestrator-image` | the earlier name of `--image orchestrator=` |
| `--service-account` | run the Job as a specific service account |
| `--kubeconfig`, `--context` | pick cluster credentials |

### --dry-run

```console
$ dokimi submit test.yaml --dry-run > manifests.yaml
Planned matrix (1 test, sequential; nodes not checked)

  worker-01 -> worker-02
    type       iperf2 over udp
    duration   30s
    bandwidth  1G

Nodes were not checked against a cluster. A dry run contacts nothing unless given --kubeconfig or --context, and then only to read.

Run id     20260924-200229-ca532a
Namespace  dokimi-iperf2
Images
  iperf2        ghcr.io/dokimi-io/iperf2@sha256:... (release-default)
  orchestrator  ghcr.io/dokimi-io/orchestrator@sha256:... (release-default)
Deadline   930s

dry run: nothing was created.
The owner uid is empty above because the API server assigns it; submission fills it in before creating anything that references it.
```

The manifests go to **stdout** and everything a person reads goes to **stderr**, so the
rendering can be saved or diffed on its own, as above.

A dry run renders from the same function `submit` uses, so the two cannot drift. By
default it is fully local and contacts no cluster at all. The one exception is described
next, and even then it only reads.

**Resolving the plan.** Given `--kubeconfig` or `--context`, a dry run also reads the
cluster's nodes and resolves the plan against them — read-only, creating nothing:

```console
$ dokimi submit test.yaml --dry-run --context kind-test > /dev/null
Planned matrix (1 test, sequential)

  pair-001  worker-01 -> worker-02
...
Both nodes exist and can accept a test pod.
```

Without either flag it does not, and says so — `Planned matrix (1 test, sequential;
nodes not checked)` — rather than implying the nodes were verified. The ambient
`KUBECONFIG` is deliberately not enough: ADR-01-06 allows a dry run "a provided
kubeconfig for read-only cluster info", and a plain `--dry-run` stays one that cannot
contact anything. This is the way to check node names before submitting.

The rendered `ownerReferences` carry an empty `uid`, which is why the output is not
directly appliable with `kubectl`. The API server assigns that uid when the owner object
is created, and submission fills it in before creating anything that points at it.

### --no-follow

Submits and returns immediately, printing how to pick the run back up:

```console
$ dokimi submit test.yaml --no-follow
submitted run 20260924-200229-ca532a to namespace dokimi-iperf2
not following. Reattach with: dokimi attach 20260924-200229-ca532a
```

The exit code is `0` for a successful submission — it says nothing about the run, which
has not finished. Use `attach` to get the verdict.

### Interrupting

Ctrl-C stops the log stream, not the run:

```console
^C
stopped following run 20260924-200229-ca532a. The run is unaffected and continues in the
cluster; resume with: dokimi attach 20260924-200229-ca532a
```

This is not special handling so much as the absence of it. The Job is a server-side
Kubernetes resource; nothing about it is tied to the client's connection. Killing the CLI
outright has the same effect.

## attach

```console
$ dokimi attach 20260924-200229-ca532a
```

Resumes streaming a run and exits with its exit code. It needs the run id and cluster
access, and nothing else — no definition file, no state left behind by whoever submitted
it. That is deliberate: the run's objects are found through their ownership labels rather
than through anything the submitting process remembered, so this works from a different
machine, and works identically for a person reattaching and for CI re-checking a run it
submitted earlier (ADR-01-06).

A run id that is not well formed is rejected before any lookup, so a typo comes back as
"malformed run id" rather than as "no such run" — which would read like the run had
finished.

Milestone-01 runs one test type, so the namespace is derivable. Pass `--namespace` if the
run is somewhere else.

## image

```console
$ dokimi image
orchestrator  ghcr.io/dokimi-io/orchestrator@sha256:ab...
  source: release-default
  digest: sha256:ab...
iperf2        ghcr.io/dokimi-io/iperf2@sha256:cd...
  source: release-default
  digest: sha256:cd...
```

Shows which images a run would submit and deploy, and where each reference came from.
`--image <component>=<reference>` works here as on `submit`. A component with no image, as
in a source checkout, is listed as such with how to choose one, and `dokimi image` still
exits `0`: it is a diagnostic, and `submit` is what refuses. See
[Choosing the orchestrator image](#choosing-the-orchestrator-image) and
[Choosing the images a run deploys](#choosing-the-images-a-run-deploys).

## Exit codes

Automation-facing from the first release, so they are an interface rather than an
implementation detail (ADR-01-08).

| Code | Meaning |
| --- | --- |
| `0` | run completed, overall pass |
| `1` | run completed, at least one non-optional pair failed |
| `2` | at least one non-optional pair errored, or a post-run anomaly |
| `3` | invalid request — a bad definition, run id or flag — detected before anything was created |
| `4` | run-level preflight failure, including another run being active (`run_already_active`) |
| `5` | internal or unexpected orchestrator error |
| `130` | you pressed Ctrl-C |

**The highest applicable code wins.** A run with both failures and errors exits `2`: an
error means part of the matrix was not validly measured, and an incomplete picture matters
more than any single finding within it.

Codes `0` to `2` are the run's own verdict. It is computed in the cluster and propagated
unchanged — the CLI does not re-derive it from what it saw in the log.

Codes `3` to `5` can come from either side of the cluster boundary. The CLI produces `3`
for a request it rejects locally, and `5` when submitting or following breaks — an API
server it cannot reach, a log stream that drops, or a bug. The run in the cluster produces
them for the same classes of problem it finds there — a definition naming a node that does
not exist, or one that is cordoned, makes the run itself exit `3` in phase 3. Either way the code is what CI sees,
so nothing but these six ever reaches it from a run:

- **A container that exits outside the table** — `137` after an out-of-memory kill, `143`
  after a `SIGTERM` — exits `5`, with the raw code and what it usually means. Passing `137`
  through would hand CI a number that means nothing here.
- **A run stopped by its deadline** exits `5` with a message saying so. The deadline is the
  Job's `activeDeadlineSeconds`; see [what a submission creates](#what-a-submission-creates).
- **A crash in the CLI itself** exits `5` with a traceback to report, rather than Python's
  default `1`, which would read as a failed test.

`130` is deliberately outside that table. Interrupting a log stream is not a verdict, and
reporting one would claim an outcome that does not exist yet.

Robot Framework's own exit codes are never propagated. It returns the number of failed
tests, which collides numerically with this table and means something entirely different.

## How nodes are resolved

Phase 3 reads the cluster's nodes **once**, at the start of the run, and plans against
that snapshot. The plan does not change if the cluster does mid-run (ADR-04-01).

Each named node must exist, and must be able to take a test pod. A node cannot if it is:

| | |
| --- | --- |
| **cordoned** | `spec.unschedulable`, set by `kubectl cordon` |
| **not Ready** | the `Ready` condition is `False`, `Unknown`, or missing |
| **tainted** | a `NoSchedule` or `NoExecute` taint — test pods carry no tolerations |

`PreferNoSchedule` is a preference, not a prohibition, and does not block. A
control-plane node is usually tainted `NoSchedule`, so naming one fails:

```
target node 'kind-control-plane' cannot accept a test pod: it has the taint
node-role.kubernetes.io/control-plane:NoSchedule, and test pods carry no tolerations
```

Every reason is reported, not just the first. A name that does not exist gets the
nearest match and the list of nodes the cluster does have:

```
target node 'worker-2' does not exist in the cluster (did you mean 'worker-02'?).
The cluster's nodes are: control-plane, worker-01, worker-02
```

All of these are `config_error`, exit `3`, and happen before anything is deployed.

Whether a node has *room* for the pod is a different question — it depends on requests,
other workloads and timing — and belongs to preflight (M1-07), which asks the scheduler
rather than guessing on its behalf.

The plan is recorded in the run's metadata: the placement mode (`explicit`), the
concurrency (`sequential`), and each pair's `test_id` and nodes. Test ids are
`pair-001`, `pair-002` and so on, in the plan's order — which is deterministic, because
nodes are sorted by name before anything is planned.

## What a submission creates

Three objects, in this order:

| Object | Purpose |
| --- | --- |
| `run-<id>-owner` (ConfigMap) | the run's owner; everything else points at it |
| `run-<id>-definition` (ConfigMap) | the authored document, mounted into the Job |
| `run-<id>` (Job) | the run itself |

They live in a namespace derived from the test type — `dokimi-iperf2` — shared
by every run of that type rather than created per run, which would need cluster-level
permission on every invocation and make cleanup wait on namespace deletion (ADR-05-02).
A run never creates the namespace (ADR-05-13): `deploy/rbac.yaml` does, once, so
submitting needs only namespaced permissions.

If a later step of the submission fails — the Job is refused, say — what it created is
deleted again, each object by name and uid after its labels are re-checked. That is different from a
failed *run*, whose objects are kept as evidence: a submission that never got as far as
a Job has no evidence to keep.

Every object carries `app.kubernetes.io/managed-by: dokimi` and
`dokimi.io/run-id: <id>`. Nothing without those labels is ever modified or
deleted, and every lookup and delete is scoped by label selector rather than by name.

The definition travels with the run in a ConfigMap rather than being re-read from your
disk. That is what makes a Job self-contained and lets `attach` need nothing local.

The Job carries `backoffLimit: 0`, `restartPolicy: Never` and an `activeDeadlineSeconds`
built from named terms: the test duration, startup, the warm-up pass, the direct read and
scrape confirmation, then preflight, diagnostics and cleanup, plus a margin. The dry run
prints it as `Deadline`. The first two are requirements, not
preferences: the exit code is the verdict, so a controller that retried on non-zero would
re-run the whole matrix on a plain failure. The deadline is what stops an orphaned run on
its own — labels make cleanup possible, deadlines make it unnecessary.

Run ids are `YYYYMMDD-HHMMSS-xxxxxx` — the UTC start time to the second, then six characters
of `[0-9a-z]` — for example `20260924-143012-k3f9x2` (ADR-02-08). Fixed width, so they sort
by start time in a log, a label and a CSV alike. Lowercase, digits and hyphens only, so a
run is spelled the same way in its labels, its object names, Prometheus and the result
artifact. `attach` refuses anything else, including a run id in capitals.

## Choosing the orchestrator image

The orchestrator is a component like the images a run deploys (ADR-04-08). Precedence,
highest first:

| Source | How | Scope |
| --- | --- | --- |
| Run override | `--image orchestrator=ghcr.io/example/orchestrator@sha256:…` | one run |
| Installation override | `DOKIMI_ORCHESTRATOR_IMAGE` | one installation |
| Release default | baked in at release time, by digest | the released build |

`--orchestrator-image` and `DOKIMI_IMAGE` are its earlier names and still work. Given
together with the new name, both have to name the same image, or `submit` refuses rather
than pick one.

A released CLI carries the digest of the image built from its own commit, so the two can
never resolve different dependency versions (ADR-05-08). A source checkout has no default
at all — see [Installation](installation.md#the-orchestrator-image).

An override is used exactly as given, including a plain tag, since an air-gapped mirror
may be all that offers one. What a run will not do is claim a pin it does not have: the
run metadata records the reference, the digest if there is one, and where the reference
came from.

## Choosing the images a run deploys

A run also deploys images of its own: today the `iperf2` test workload. Each one is chosen
the same way as the orchestrator's, per component (ADR-04-08):

| Source | How | Scope |
| --- | --- | --- |
| Run override | `--image iperf2=registry.internal/iperf2@sha256:…`, repeatable | one run |
| Installation override | `DOKIMI_IPERF2_IMAGE`, on the machine that submits | one installation |
| Release default | baked in at release time, by digest | the released build |

They are chosen **at submission, never in the test document.** An image depends on where a
run is submitted, not on what it tests: the same document runs unchanged in every cluster,
while each site may pull from its own mirror. `submit` resolves every image and writes it,
with its source, into the Job's environment (`DOKIMI_IPERF2_IMAGE` and
`DOKIMI_IPERF2_IMAGE_SOURCE`), so `--dry-run` shows exactly what will run. The Job uses what
it was given and never reads the submitter's environment, which does not reach it anyway.

A source checkout has no default for the images dokimi publishes itself, the orchestrator
and `iperf2`, since nothing of this code was published. `submit` refuses before creating
anything:

```console
$ dokimi submit test.yaml
dokimi: this is a development build with no published iperf2 image, so there is no
default to run. Choose one with --image iperf2=<reference> or DOKIMI_IPERF2_IMAGE
```

`dokimi image` shows every image a run would use and where each one came from.

## Cleaning up

Automatic cleanup arrives with M1-16 (#51). Until then, deleting a run's owner object
takes the rest with it — the cluster's own garbage collector cascades through the
`ownerReferences`:

```console
$ kubectl delete configmap run-20260924-200229-ca532a-owner -n dokimi-iperf2
```

Or by label, across everything a run created:

```console
$ kubectl delete job,configmap -n dokimi-iperf2 \
    -l dokimi.io/run-id=20260924-200229-ca532a
```

The run id reads the same in the object name and the label, so either command can be
built from the id `submit` printed.
