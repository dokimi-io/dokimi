# Usage

The CLI is phase 1 of a run and the only part that executes outside the cluster. It
validates the definition, renders the manifests, submits the Job and streams its logs
back. Everything after that happens in the cluster.

```console
$ dokimi validate test.yaml          # parse and check, no cluster involved
$ dokimi convert test.yaml           # print the Robot suite a run executes
$ dokimi submit test.yaml --dry-run  # render the matrix and the manifests
$ dokimi submit test.yaml            # create the run and stream its logs
$ dokimi preflight test.yaml         # check the cluster for that run, and run no test
$ dokimi attach <run-id>             # resume streaming a run already going
$ dokimi prometheus <run-id>         # open the run's kept Prometheus on a local port
$ dokimi completion zsh              # print the tab-completion script for your shell
```

- [What a run does today](#what-a-run-does-today)
- [Before the first run](#before-the-first-run)
- [validate](#validate)
- [convert](#convert)
- [submit](#submit)
- [preflight](#preflight)
- [attach](#attach)
- [prometheus](#prometheus)
- [image](#image)
- [completion](#completion)
- [The run's reports](#the-runs-reports)
- [Exit codes](#exit-codes)
- [How nodes are resolved](#how-nodes-are-resolved)
- [What a submission creates](#what-a-submission-creates)
- [Choosing the orchestrator image](#choosing-the-orchestrator-image)
- [Choosing the images a run deploys](#choosing-the-images-a-run-deploys)
- [Naming the cluster](#naming-the-cluster)
- [Cleaning up](#cleaning-up)

## What a run does today

A run goes from submission to its verdict, and leaves its results in files. It parses its definition, resolves its two nodes against the
cluster, plans the pair, claims the cluster, starts its own Prometheus, with a
node-exporter when the cluster has none, and checks that it answers. It checks that the
cluster can run the pair, and then **runs the test through Robot Framework**: it renders
the suite, deploys the pair's pods, waits until both are Ready, starts the measurement and
waits for it to finish. It reads the result directly from the pair's exporters, confirms
Prometheus holds the same values, and evaluates the pass criteria. Then it records where
everything ran, removes what it doesn't keep, and exits with its verdict:

```
run 20261002-120000-a1b2c3
phase 2 (ingestion): definition parsed
...
phase 3 (planning): 1 test, sequential
  pair-001  worker-01 -> worker-02
phase 4 (preflight): cluster claimed for this run until 2026-10-02T12:20:30Z
phase 4 (preflight): monitoring mode managed (requested)
  Prometheus  http://run-20261002-120000-a1b2c3-prometheus.dokimi-iperf2.svc:9090
  node_exporter       deployed by this run (dokimi-iperf2/run-20261002-120000-a1b2c3-node-exporter)
  kube_state_metrics  absent
phase 4 (preflight): Prometheus answers queries
  node-exporter pods  4 of 4 pods ready
  kept after the run; remove it with: kubectl delete deployment,service,configmap -n dokimi-iperf2 -l ...
  query it from your machine with: dokimi prometheus 20261002-120000-a1b2c3 --namespace dokimi-iperf2
phase 4 (preflight): nodes worker-01, worker-02 accept test pods
phase 4 (preflight): pair-001 scheduled, pulled its image and connected, as run-20261002-120000-a1b2c3-pair-001-preflight-client, run-20261002-120000-a1b2c3-pair-001-preflight-server
  UDP to the server pod at 10.244.2.7:5001 arrived, and kubernetes.default.svc resolved
phase 4 (preflight): removed 2 test pod(s)
phase 4 (preflight): the cluster is ready

phase 5 (suite rendering): /work/artifacts/20261002-120000-a1b2c3/suite.robot
phase 6 (execution): pair-001  worker-01 -> worker-02, one Robot Framework process
...
phase 7 (measurement): pair-001, read directly from its exporters (inbound): jitter_seconds 1.1e-05, packet_loss_percent 0, test_duration_seconds 30, throughput_bits_per_second 9.49e+08, transferred_bytes 3.56e+09
phase 7 (measurement): Prometheus holds the series of pair-001

phase 8 (validation): pair-001 fail, 2 criterion(s)
  fail  Throughput Should Be At Least 950 megabits per second: 949 megabits per second
  pass  Packet Loss Should Be At Most 0 percent: 0 percent

phase 9a (reporting): /work/artifacts/20261002-120000-a1b2c3/log.html, .../report.html
phase 9b (results): 1 test(s) in tests.csv and tests.jsonl, 2 assertion(s) in assertions.csv and assertions.jsonl

phase 10 (diagnostics): 5 pod(s), 1 pair(s), none down
  run-20261002-120000-a1b2c3-pair-001-client on worker-01: Running
  run-20261002-120000-a1b2c3-pair-001-server on worker-02: Running
  run-20261002-120000-a1b2c3-prometheus-7d9f6c5b8-x2kqp on worker-03: Running
  2 node-exporter pod(s)
phase 10 (diagnostics): /work/artifacts/20261002-120000-a1b2c3/diagnostics.json

phase 11 (cleanup): removed node-exporter run-20261002-120000-a1b2c3-node-exporter
phase 11 (cleanup): run kept for inspection (fail): run-20261002-120000-a1b2c3-pair-001-client, run-20261002-120000-a1b2c3-pair-001-server, and its owner and definition. It no longer holds the cluster. Remove it with: dokimi cleanup 20261002-120000-a1b2c3 --namespace dokimi-iperf2
```

It exits `1`: a criterion did not hold. A run whose tests all pass exits `0`, and one with
a test that couldn't be validly measured exits `2`, whatever the others found. See
[Exit codes](#exit-codes). A node that is missing or cannot take a test pod stops it
earlier, in phase 3, with `3`. A cluster that cannot run the pair stops it in phase 4,
with `4`, before any test: see [preflight](#preflight).

Six things to know about a run:

- **The result is read directly from the exporters, and Prometheus only confirms it.**
  Once a test has finished, each of its pods' exporters is read over `/metrics`, the same
  surface Prometheus scrapes. The receiver's view, `direction="inbound"`, is the result.
  An exporter that serves no completed result fails the test as `metrics_missing`, one
  that serves something unreadable as `exporter_parse`; neither is ever a pass. Then the
  run asks Prometheus for the same series, for up to a minute (three scrape intervals, at
  least 60 s). If they never arrive, the run warns `HISTORY NOT CAPTURED` and goes on: the
  result stands, only its history in Prometheus is missing. A value Prometheus holds
  differently is reported on its own. Both land in `measurement.json`.
- **Every pass criterion is checked against what the pair measured** (phase 8), in the
  unit you wrote it in: `950 megabits per second` is compared with a throughput measured in
  bits per second, and throughput prefixes are decimal. A criterion that doesn't hold
  fails, naming the pair and its value in your unit. One that couldn't be checked fails as
  `ERROR` with its category, which is never a pass and never a finding about the network:
  `metrics_missing` if the pair has no such value, `exporter_parse` if it isn't a number,
  and a metric the test type never produces, such as packet loss over TCP, is said once.
  Every criterion is checked and listed, not only the first. Robot's report tags each test
  `outcome:pass`, `outcome:fail` or `outcome:error`, an error also `error:<category>`, and
  each test's outcome is in `pairs/<test-id>.validation.json`, with the PromQL that
  reproduces it from the run's Prometheus: see [validation.json](#validationjson).
- **Robot's report comes back to you.** `submit` and `attach` extract it into
  `<results.directory>/<run-id>/`: `log.html`, `report.html`, the merged `output.xml`, and
  `suite.robot`, the exact suite that ran. See [The run's reports](#the-runs-reports).
- **Before anything is removed, the run records itself** (phase 10): which
  node every pod ran on, how each pod and container ended, which pairs were down, and
  whether the namespace was still reachable. That's `diagnostics.json`, next to Robot's
  reports, on every run. Anything no test was in flight to notice is an anomaly: a test
  pod or the Prometheus pod that went missing, was evicted or stopped by its deadline, a
  container of one that restarted, crash-looped or was OOM-killed, or its node no longer
  Ready. The node-exporter's pods, one on every node, are recorded but never an anomaly.
  An anomaly marks the run `"retain": true`, and makes it exit `2` even if its tests passed:
  something went wrong around them, so their results are not the whole picture.
- **A run that didn't pass keeps its evidence, without holding the cluster** (phase 11).
  A run that exits `0` removes its test pods and its node-exporter, and once `submit` or
  `attach` has fetched its reports, its Job, owner and definition go too. A run that
  doesn't exit `0` keeps its test pods, with their logs and events, and its owner and
  definition, for you to look at; it says so, and how to remove it: `dokimi cleanup
  <run-id>`. Either way it is marked finished, so the next run can start. `submit --retain
  always` keeps a passing run too, and `--retain never` keeps nothing. A run that stopped
  in its preflight keeps nothing either: there is nothing of it to look at but its log and
  `diagnostics.json`, and those reach you.
- **A test pod that may still be sending never stays.** The next run would measure next to
  it. So a test's pods are kept only if it was measured, or its pods never started: a
  test that timed out, lost a pod or recorded nothing has them removed, however the run
  ended, and the run waits until they are gone. If they can't be removed, the run keeps
  its claim until it expires, and says how to free the cluster earlier: `dokimi cleanup
  <run-id> --force`, once its Job has ended.
- **Its Prometheus stays after the run,** with the node metrics it scraped while the run was
  on. The node-exporter the run deployed is removed, also from a run that keeps its
  evidence, or the Prometheus would go on scraping every node through it. Phase 4 prints how
  to query the Prometheus and how to remove it; see [Cleaning up](#cleaning-up).

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
`dokimi-iperf2` `get` and `update` on ConfigMaps, `get` on Jobs, `create` on ConfigMaps,
Services and Deployments for the run's managed Prometheus, `create`, `get`, `list` and `delete`
on DaemonSets for the node-exporter a run deploys when the cluster has none and removes when it
ends, and `create`, `get`, `list` and `delete` on Pods: each pair's client and server, removed
again when the run ends. It also creates the `dokimi-prometheus` service account that Prometheus
runs as, which can read pods, services and endpoint slices for discovery. Then submit with that
service account:

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

A test also needs three network paths inside `dokimi-iperf2`. If a NetworkPolicy applies
there, it has to allow all three:

- **the Job to each client pod on 5101/TCP.** That is how a test is started: the Job
  connects and sends the server's address, instead of running a command inside the pod;
- **the client pod to the server pod on 5001,** over the test's protocol, UDP or TCP;
- **the Job to each test pod's exporter on 9101/TCP.** That is how the result is read in
  phase 7. Phase 4 cannot check this path, because the probe's pods run without the
  exporter, so a cluster that blocks it fails every test as `metrics_missing`.

Test traffic uses the pod network and IPv4, so a pod network without IPv4 cannot run a
test. Phase 4 checks the first two paths and the address family before any test runs, and that a
pod can resolve a cluster name through the cluster's DNS: see [preflight](#preflight).

The run's managed Prometheus adds three more: the Job to the Prometheus on 9090/TCP, the
Prometheus to each test pod's exporter on 9101/TCP, and the Prometheus to the
node-exporter on 9137/TCP on each node's own address, if the run deployed one. Phase 4
checks the first and warns about the last; node metrics explain a result and never decide
one, so a run continues without them.

The service account exists only in `dokimi-iperf2`, the namespace
`deploy/rbac.yaml` creates. A run never creates a namespace, so one submitted with
`--namespace` somewhere else needs that namespace to exist already, and will not find the
service account there. For another namespace, copy the file, change the namespace in the
run `Namespace`, the `ServiceAccount`, the namespaced `Role` and every binding's subject,
and apply that copy. Leave the `dokimi` lock namespace as it is: there is one lock per
cluster.

## One run at a time

A cluster runs one run at a time, whatever its namespace or test type: two runs would
contend for the same network they measure. The Job claims the cluster as the
first step of phase 4. A second run started meanwhile is refused and exits `4`:

```
dokimi: refusing to start run 20260924-201500-p7m2q8 (run_already_active): run 20260924-200229-ca532a already active in this cluster.
  - ConfigMap dokimi-iperf2/run-20260924-200229-ca532a-owner claimed at 2026-09-24T20:02:41Z, claim expires at 2026-09-24T20:25:11Z
Wait for it to finish, or for its claim to expire. If it is stale, because its Job is gone, clear it, which removes what it created with it:
  dokimi cleanup 20260924-200229-ca532a --namespace dokimi-iperf2 --force
```

Only a run that has claimed the cluster counts. A run stops counting:

- **when it ends**, however it ends: once no test pod of it can still be sending, it marks
  itself finished, so a failed run whose objects are kept as evidence does not hold the
  cluster;
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
the other. See the [repository README](../README.md#authoring-a-test) for the
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
calls the same function, so what you see here is byte for byte what Robot
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
| `--cluster` | the cluster's name, which labels every result; required, see [Naming the cluster](#naming-the-cluster) |
| `--image <component>=<reference>` | choose an image for this run, repeatable: `orchestrator`, the Job's own, or `iperf2` |
| `--orchestrator-image` | the earlier name of `--image orchestrator=` |
| `--service-account` | run the Job as a specific service account |
| `--retain` | when the run keeps its test pods and record for inspection: `on-failure` (the default), `always` or `never`; see [Cleaning up](#cleaning-up) |
| `--kubeconfig`, `--context` | pick cluster credentials |

Once it has extracted the run's reports, `submit` removes the run's Job, which has nothing
left to give. A run that kept nothing goes completely, its owner and definition with it;
one that kept its evidence keeps it for `dokimi cleanup`.

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
Cluster    lab-fra-1
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
`KUBECONFIG` is deliberately not enough: a dry run touches a cluster only through a
kubeconfig or context you name, and then only reads, so a plain `--dry-run` stays one that
cannot contact anything. This is the way to check node names before submitting.

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

## preflight

```console
$ dokimi preflight test.yaml --service-account dokimi
```

Checks that the cluster can run the definition's tests, and runs none of them. It submits
the same run `submit` would, whose Job stops after phase 4, so it checks exactly what a run
checks, with the same code. It exits `0` if the cluster is ready and `4` if not,
with the check that failed:

```
dokimi: cluster not ready (connectivity): the probe of pair-001 (worker-01 -> worker-02): no UDP to the server pod at 10.244.2.7:5001 arrived
```

Phase 4 runs these checks in this order, in every run, and stops at the first that fails:

| Check | What failed | Where to look |
| --- | --- | --- |
| `run_already_active` | another run holds the cluster | [One run at a time](#one-run-at-a-time) |
| `prometheus_unreachable` | the run's Prometheus did not answer a query within 180 s | its pod, and the Job's path to it on 9090/TCP |
| `node_count` | a node the plan needs is gone, or no longer takes a test pod, since phase 3 read it | `kubectl get nodes` |
| `scheduling` | a pod of the first pair could not be placed | its events: resources, taints, quotas |
| `image_pull` | a pod of the first pair was placed, and could not get its image | the image reference, the registry, the pull credentials |
| `connectivity` | the Job could not reach the client pod, or no traffic from the client reached the server | NetworkPolicies, the CNI, an IPv4 pod network |
| `dns` | the pod network works, and `kubernetes.default.svc` did not resolve inside the client pod | the cluster's DNS |

The scheduling, image and network checks reuse the run's first pair: its two pods, on its
two nodes, from its image. The only difference is the client's start: instead of
warming up and measuring, it sends one second of traffic at 10 Mbit/s to the server and
resolves `kubernetes.default.svc`. Both pods are removed before phase 5, and the test
deploys the pair afresh, so the probe never runs next to a test.

`dokimi preflight` is not a preview. `submit --dry-run` renders what a run would create and
creates nothing; this creates real pods, because whether they schedule and connect can
only be seen by trying. It claims the cluster while it runs, provisions the run's managed
Prometheus, which is kept as every run's is, and removes everything else it deployed,
whether it passed or not. Like every run, it ends with phase 10's snapshot:
`diagnostics.json` in `<results.directory>/<run-id>/`, which after a failed check shows the
probe's pods as they were. Whatever the snapshot finds is in the log and that file, and
doesn't change the exit code: a preflight answers `0` or `4`.

A passed preflight is a floor, not a guarantee: one pair, under no load, says nothing about
what the full matrix will meet.

`preflight` takes the same flags as `submit`, except `--dry-run` and `--retain`.

## attach

```console
$ dokimi attach 20260924-200229-ca532a
```

Resumes streaming a run and exits with its exit code. It needs the run id and cluster
access, and nothing else — no definition file, no state left behind by whoever submitted
it. That is deliberate: the run's objects are found through their ownership labels rather
than through anything the submitting process remembered, so this works from a different
machine, and works identically for a person reattaching and for CI re-checking a run it
submitted earlier.

A run id that is not well formed is rejected before any lookup, so a typo comes back as
"malformed run id" rather than as "no such run" — which would read like the run had
finished.

Milestone-01 runs one test type, so the namespace is derivable. Pass `--namespace` if the
run is somewhere else.

`attach` also extracts the run's reports, into the `results.directory` of the definition
the run was submitted with (see [The run's reports](#the-runs-reports)). Reading that
definition takes `get` on the run's ConfigMaps. `--results-directory <dir>` extracts there
instead, and needs no such read. Like `submit`, it then removes the run's Job, so a run's
reports can be fetched once.

## prometheus

```console
$ dokimi prometheus 20260924-200229-ca532a
run 20260924-200229-ca532a's Prometheus: http://127.0.0.1:54817
  the run started 2026-09-24T20:02:29Z, ended 2026-09-24T20:05:41Z
  its series carry run_id="20260924-200229-ca532a"
  forwarding until you press Ctrl-C, or kill 48213
```

Opens the Prometheus a run kept, from your machine: it listens on a local port and
forwards every connection to the run's Prometheus pod, until you press Ctrl-C. Open the
URL in a browser, or point a Grafana data source or `curl` at it. Every series the run
produced carries its `run_id`, and the time range says which window holds them. A run
whose owner is already gone, because it kept nothing and its reports were fetched, still
has its start in its id; its end isn't recorded any more, so query from its start.

Query at the run's time, not now. Prometheus marks a series stale five minutes after its
last sample, so an instant query for now finds nothing once the run is that old. Give the
time the command printed:

```console
$ curl -s 'http://127.0.0.1:54817/api/v1/query' \
    --data-urlencode 'query=dokimi_throughput_bits_per_second{run_id="20260924-200229-ca532a"}' \
    --data-urlencode 'time=2026-09-24T20:05:41Z'
```

To keep your terminal, run it in the background with `&`. Ctrl-C doesn't reach a
background job, so stop it with `kill %1`, or `kill` and the process id it printed, or
bring it back with `fg` and press Ctrl-C. Either way it stops the same: it closes the local
port, says "stopped forwarding" and exits `0`. Nothing is left behind in the cluster: a
port-forward isn't an object there.

`--port <n>` listens on that port instead of a free one. Like `attach`, it finds the run
by its labels in its namespace, so it works from any machine; pass `--namespace` if the
run is somewhere else.

It needs no `kubectl`. Each connection opens a port-forward of its own, which takes
`create` on `pods/portforward` in the run's namespace, besides `list` on Deployments and
Pods and `get` on ConfigMaps to find the run. It asks the API server whether it may
before it listens, as `kubectl auth can-i create pods/portforward` does. If it may not, it
says so and exits `3`. A port-forward that fails later is reported per connection, and
forwarding goes on.

A run whose Prometheus was removed, with `dokimi cleanup --prometheus` or by hand, or that
never deployed one, says so and exits `3`, as `attach` does for a run it can't find. A
Prometheus whose pod isn't ready exits `5`.

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

## completion

```console
$ eval "$(dokimi completion bash)"
$ source <(dokimi completion zsh)
$ dokimi completion fish | source
```

Prints a script that makes your shell complete `dokimi`: its commands, every flag, the values a
flag accepts from a fixed set (`--retain`, `--to`, the shells here), the components `--image`
takes, and files. A definition is completed from `.yaml`, `.yml` and `.robot` files and
directories, `--kubeconfig` from files, and `--results-directory` from directories. Nothing that
only the cluster knows is completed, such as run ids, namespaces or contexts, and nothing is
offered for a value only you know, such as `--run-id`.

The script is generated from the CLI's own parser, so it matches the version that printed it.
It runs in the shell alone: no Python starts when you press tab.

The lines above load it into the current shell. To have it in every new shell, either load it at
start-up or save it once:

| Shell | Load it at start-up | Or save it once |
| --- | --- | --- |
| bash | `eval "$(dokimi completion bash)"` in `~/.bashrc` | `dokimi completion bash > ~/.local/share/bash-completion/completions/dokimi`, with the bash-completion package installed |
| zsh | `source <(dokimi completion zsh)` in `~/.zshrc`, after `compinit` | `mkdir -p ~/.zfunc` and `dokimi completion zsh > ~/.zfunc/_dokimi`, with `fpath=(~/.zfunc $fpath)` in `~/.zshrc` before `compinit` |
| fish | `dokimi completion fish \| source` in `~/.config/fish/config.fish` | `dokimi completion fish > ~/.config/fish/completions/dokimi.fish` |

A saved script stays as it was, so save it again after upgrading dokimi. Loading it at start-up
always matches the installed version.

It completes the `dokimi` command, so it needs `dokimi` on your `PATH`, as
`uv tool install .` puts it there ([Installation](installation.md#installing)). `uv run dokimi`
is not completed.

On bash 3.2, the version macOS ships, use `eval` as above: there, `source <(...)` silently reads
nothing. Directories are offered without their trailing slash on bash 3.2.

## The run's reports

A run writes its artifacts inside the Job, which disappears with its pod. So at the end, the
Job writes them into its own log, as one checksummed block, and `submit` or `attach` takes
that block out of what it prints and extracts it into the definition's `results.directory`,
under the run id:

```
results/20261002-120000-a1b2c3/
  log.html       Robot Framework's log of the run
  report.html    its report
  output.xml     every pair's results, merged by rebot
  suite.robot    the suite that ran, byte for byte what dokimi convert prints
  pairs/         one output.xml per pair, what its test read directly
                 (<test-id>.measurement.json), and what its criteria came to
                 (<test-id>.validation.json)
  tests.csv, tests.jsonl            one record per test: what it measured and came to
  assertions.csv, assertions.jsonl  one record per criterion and pair, passing ones included
  run.json          the run's record: its configuration, images, outcome and cleanup
  measurement.json  every test's directly read values, and what Prometheus held of them
  diagnostics.json  where every pod ran, how it ended, and any anomaly (phase 10)
```

`attach` reads `results.directory` from the definition the run was submitted with. A
relative directory, like the default `./results`, is relative to wherever `attach` runs,
so it only lands in the same place from any machine when the definition gives an absolute
one. `attach --results-directory <dir>` puts it somewhere else, and skips reading the
definition.

A block that is truncated, fails its checksum, or holds anything that would land outside
that directory is reported, and nothing is extracted. A run whose log carries no block at
all, because it stopped before writing one or because its log was rotated, says so. None
of this changes the run's exit code.

Two consequences of carrying them in the log:

- **A run's artifacts can be fetched once, within an hour after it ended.** `submit` or
  `attach` removes the run's Job once it has extracted them, and the cluster removes a Job
  nobody fetched an hour after it finished (`ttlSecondsAfterFinished: 3600`). `submit
  --no-follow` extracts nothing; `dokimi attach <run-id>` later does, within that hour. A
  block that didn't arrive whole leaves the Job in place, so you can reattach.
- **The Job's raw log ends with a block of base64.** `kubectl logs` shows it; `submit` and
  `attach` never do.

### The result files

Every run writes its results, without being asked: one record per test, and one per
criterion and pair, each as CSV and as JSONL, the same records serialized twice. They are
written from what each test itself recorded, its directly read measurement and what its
criteria came to, so a value in them is the value the verdict was computed from. Each
carries `schema`, `1` today: a column may be added at the end, and removing, renaming or
reordering one raises it. An empty CSV cell is a JSON `null`. A metric an exporter reported
as `NaN` or `Inf` is empty too, since JSON has no such number; its test is an `error` with
the category `exporter_parse`.

`tests.csv` and `tests.jsonl`, one record per planned test, whatever it came to:

| Column | What it holds |
| --- | --- |
| `schema`, `run_id`, `cluster`, `run_started_at` | The record's version, and the run |
| `test_id`, `repetition`, `test_type`, `protocol`, `source_node`, `target_node` | The test |
| `duration_seconds`, `target_bandwidth_bits_per_second` | What the definition asked for |
| `outcome`, `error_category`, `error_detail` | `pass`, `fail` or `error`, and for an error why: its category, or its criteria's |
| `measurement_source`, `measurement_direction`, `history_captured` | Where the values come from (`direct`, `inbound`), and whether Prometheus came to hold them |
| `jitter_seconds`, `packet_loss_percent`, `test_duration_seconds`, `throughput_bits_per_second`, `transferred_bytes` | Every result metric, in base units, empty where the test has none |

`assertions.csv` and `assertions.jsonl`, one record per criterion and pair, passing or not:

| Column | What it holds |
| --- | --- |
| `schema`, `run_id`, `cluster`, `test_id`, `source_node`, `target_node` | The record's version, the run and the pair |
| `criterion` | The criterion as the suite writes it |
| `metric`, `statistic`, `comparison`, `threshold`, `unit` | The criterion's fields; the threshold as written |
| `measured` | The measured value in the criterion's unit, as a decimal string: exactly what was compared |
| `outcome`, `error_category`, `error_detail` | `pass`, `fail` or `error`, and for an error why |
| `promql_equivalent` | How to find the pairs that violate it in the run's Prometheus |

`run.json`, written last, once the run's verdict and cleanup are known:

| Field | What it holds |
| --- | --- |
| `schema`, `run_id`, `cluster`, `namespace`, `started_at`, `ended_at`, `preflight_only` | The run |
| `outcome`, `exit_code` | What it came to; both `null` for a run that stopped on an error, and `stopped` says which |
| `tests` | How many tests were planned, and how many passed, failed and errored |
| `retained` | Why the run kept its evidence, or `null` |
| `cleanup` | `complete`, and if not, the `cleanup` category and the problem; the test pods it kept |
| `configuration` | The effective configuration: the definition as YAML, `--retain`, the namespace and the cluster's name |
| `images` | Each image it ran: its reference, its digest where it is pinned by one, and where it came from |
| `plan`, `monitoring`, `scrape_targets` | The plan, the Prometheus it used, and how many of each scrape pool's targets were up or down |

### validation.json

`pairs/<test-id>.validation.json`: what a test's pass criteria came to, phase 8, written by
its Robot process next to its `output.xml`. It is the classification itself; Robot's
status and tags are only a rendering of it. Its `schema` is `1`; a field that changes
meaning or goes away raises it.

| Field | Type | What it says |
| --- | --- | --- |
| `schema` | integer | `1` |
| `test_id` | string | The test |
| `outcome` | string | `pass`, `fail` or `error`: the worst of its criteria, and `error` if it has none, or if it ended before them |
| `error` | object or null | Why the test ended before its criteria: `category` (below, or `null` for an error nothing anticipated) and `detail`. `null` for a test that reached them |
| `criteria` | list | Each criterion, in the order the suite checks them, as below. Empty for a test that ended before them |

Each criterion:

| Field | Type | What it says |
| --- | --- | --- |
| `phrase` | string | The criterion as the suite writes it: `Throughput Should Be At Least 950 megabits per second` |
| `metric`, `statistic`, `comparison`, `threshold`, `unit` | string | The criterion's fields; `statistic` is `null` where the metric has none, and `threshold` is the number as written |
| `outcome` | string | `pass`, `fail` or `error`: the worst of its pairs |
| `categories` | list | For an `error`, each error category once: `metrics_missing`, `exporter_parse` |

The error categories, here and in Robot's `error:<category>` tags:

| Category | The test |
| --- | --- |
| `scheduling` | had a pod that could not be placed on its node |
| `image_pull` | had a pod that could not get its image |
| `tool_execution` | ran its tool, which measured nothing, measured another path than the pair's, or lost a pod |
| `timeout` | did not finish in the time it was given |
| `metrics_missing` | finished, and an exporter served no result for it |
| `exporter_parse` | finished, and an exporter served something that is not a result |
| `promql_equivalent` | string | The PromQL that returns the pairs violating it from the run's Prometheus. An equivalent for checking by hand, not what decided: the verdict comes from the direct read |
| `pairs` | list | Each expected pair: `test_id`, `source_node`, `target_node`, `outcome`, `value` (the measured value as a decimal string in the criterion's unit, or `null` for an error), `category` and `detail` (for an error) |

The three outcomes:

| `outcome` | Means |
| --- | --- |
| `pass` | Measured, and the criterion holds for every expected pair |
| `fail` | Measured, and it doesn't hold for at least one pair: a finding about the network |
| `error` | At least one pair couldn't be validly measured, and nothing about the network follows from it. Never a pass, and it outranks `fail` |

### diagnostics.json

The post-run snapshot, taken before anything of the run is removed. Its `schema` is `1`;
a field that changes meaning or goes away raises it.

| Field | Type | What it says |
| --- | --- | --- |
| `schema` | integer | `1` |
| `run_id`, `cluster`, `namespace` | string | The run, the cluster's name as given at submission, and the namespace it ran in |
| `captured_at` | string | When the snapshot began, UTC, ISO 8601 |
| `complete` | boolean | `false` if a step failed or the snapshot ran out of time; the rest is still there |
| `retain` | boolean | `true` if there is any anomaly: the run keeps its evidence, and exits `2` |
| `anomalies` | list | Each with `kind`, `subject` (a pod, a node, a pair as `<test-id>/<role>`, or `run`) and `detail` |
| `prometheus_deployed` | boolean | Whether the run deployed a Prometheus of its own |
| `reachable.namespace` | boolean or null | Whether the run's pods could be listed; `null` if not tried |
| `reachable.prometheus` | boolean or null | Whether the run's Prometheus was ready; `null` if it deployed none, or the check didn't run |
| `pairs` | list | Each planned pair: `test_id`, `source_node`, `target_node`, `started` (its pods were deployed), `pods` (each role's pod name, or `null`), `findings` (its Robot output, relative to the run's directory, or `null`) and `down` |
| `pods` | list | Each run-owned pod: `name`, `component`, `test_id`, `node`, `phase`, `ready`, `ip`, `reason`, `message`, and `containers`, each with `name`, `state`, `reason`, `exit_code`, `restarts`, `last_state`, `last_reason` and `last_exit_code` |

The anomaly kinds:

| `kind` | When |
| --- | --- |
| `pod_missing` | A pod of a pair whose pods were deployed no longer exists |
| `pod_evicted`, `pod_deadline_exceeded` | The cluster ended a test pod or the Prometheus pod |
| `container_restarted`, `container_crash_looping`, `container_oom_killed` | A container of a test pod or the Prometheus pod, also an OOM kill before a restart |
| `node_not_ready` | The node of a test pod or the Prometheus pod is not `Ready`, or gone |
| `namespace_unreachable`, `prometheus_unreachable` | The run's pods couldn't be listed, or its Prometheus wasn't ready |
| `snapshot_incomplete` | A step failed (its `subject` names it) or the snapshot ran out of time (`run`) |

A pair is `down` when its pods were deployed and one is missing, a container of it exited
non-zero, or an anomaly concerns its pods or its nodes. A client that exited non-zero is
the test's own result, so it is not an anomaly, but its pair is down.

## Exit codes

Automation-facing from the first release, so they are an interface rather than an
implementation detail.

| Code | Meaning |
| --- | --- |
| `0` | run completed, overall pass |
| `1` | run completed, at least one test failed a criterion |
| `2` | at least one test could not be validly measured, or a post-run anomaly |
| `3` | invalid request — a bad definition, run id or flag — detected before anything was created |
| `4` | run-level preflight failure: another run being active (`run_already_active`), or a cluster check (`cluster not ready (<check>)`, see [preflight](#preflight)) |
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
that snapshot. The plan does not change if the cluster does mid-run.

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
other workloads and timing — and phase 4 answers it by asking the scheduler rather than
guessing on its behalf: it deploys the first pair once ([preflight](#preflight)). Phase 4
also reads the nodes again, so a node lost between planning and the tests is a
`node_count` failure, exit `4`, rather than a test that never starts.

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
permission on every invocation and make cleanup wait on namespace deletion.
A run never creates the namespace: `deploy/rbac.yaml` does, once, so
submitting needs only namespaced permissions.

If a later step of the submission fails — the Job is refused, say — what it created is
deleted again, each object by name and uid after its labels are re-checked. That is different from a
failed *run*, whose test pods and record are kept as evidence: a submission that never got
as far as a Job has no evidence to keep.

Every object carries `app.kubernetes.io/managed-by: dokimi` and
`dokimi.io/run-id: <id>`. Nothing without those labels is ever modified or
deleted, and every lookup and delete is scoped by label selector rather than by name.

The definition travels with the run in a ConfigMap rather than being re-read from your
disk. That is what makes a Job self-contained and lets `attach` need nothing local.

The Job carries `backoffLimit: 0`, `restartPolicy: Never`, `ttlSecondsAfterFinished: 3600`,
so that the cluster removes a finished Job nobody fetched the reports of within the hour,
and an `activeDeadlineSeconds` built from named terms: the test duration, startup, the warm-up pass, the direct read and
scrape confirmation, then preflight, diagnostics and cleanup, plus a margin. The dry run
prints it as `Deadline`. The first two are requirements, not
preferences: the exit code is the verdict, so a controller that retried on non-zero would
re-run the whole matrix on a plain failure. The deadline is what stops an orphaned run on
its own — labels make cleanup possible, deadlines make it unnecessary.

Run ids are `YYYYMMDD-HHMMSS-xxxxxx` — the UTC start time to the second, then six characters
of `[0-9a-z]` — for example `20260924-143012-k3f9x2`. Fixed width, so they sort
by start time in a log, a label and a CSV alike. Lowercase, digits and hyphens only, so a
run is spelled the same way in its labels, its object names, Prometheus and the result
artifact. `attach` refuses anything else, including a run id in capitals.

## Choosing the orchestrator image

The orchestrator is a component like the images a run deploys. Precedence,
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
never resolve different dependency versions. A source checkout has no default
at all — see [Installation](installation.md#the-orchestrator-image).

An override is used exactly as given, including a plain tag, since an air-gapped mirror
may be all that offers one. What a run will not do is claim a pin it does not have: the
run metadata records the reference, the digest if there is one, and where the reference
came from.

## Choosing the images a run deploys

A run also deploys images of its own: the `iperf2` test workload, its managed `prometheus`,
and a `node-exporter` when the cluster has none. Each one is chosen the same way as the
orchestrator's, per component:

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

`prometheus` and `node-exporter` are upstream images, so their default is a digest pinned in
the code, also in a source checkout. They are resolved only when the run may deploy them, for
the `managed` and `auto` monitoring modes. A source checkout has no default for the images
dokimi publishes itself, the orchestrator and `iperf2`, since nothing of this code was
published. `submit` refuses before creating
anything:

```console
$ dokimi submit test.yaml
dokimi: this is a development build with no published iperf2 image, so there is no
default to run. Choose one with --image iperf2=<reference> or DOKIMI_IPERF2_IMAGE
```

`dokimi image` shows every image a run would use and where each one came from.

## Naming the cluster

Every result is labelled with the cluster it was measured on, so that history from two
clusters is never mixed up. dokimi does not guess that name: not from the
kubeconfig context, not from anything in the cluster. You give it, and there is no default:

| Source | How | Scope |
| --- | --- | --- |
| Run override | `--cluster lab-fra-1` | one run |
| Installation override | `DOKIMI_CLUSTER=lab-fra-1`, on the machine that submits | one installation |

Without either, `submit` and `preflight` refuse before creating anything, a dry run included:

```console
$ dokimi submit test.yaml
dokimi: name the cluster this run executes against with --cluster <name> or DOKIMI_CLUSTER.
It labels every result, and has no default, so that results from different clusters are
never merged
```

The name is a DNS label: lowercase letters, digits and hyphens, starting and ending with a
letter or digit, at most 63 characters. Anything else is refused rather than rewritten, so
`Prod-EU` never quietly becomes someone else's `prod-eu`. Like the images, `submit` writes
it into the Job (`DOKIMI_CLUSTER`), and `--dry-run` shows it.

**It only names the cluster; it doesn't choose it.** Unlike kubectl's `--cluster`, it never
selects which cluster the run goes to: `--kubeconfig` and `--context` do that (see
[Installation](installation.md#cluster-credentials)). So `--cluster prod` with a staging context
runs on staging and labels the results `prod`, and nothing notices.

It is what you called the cluster, not an identity dokimi checks: two people can name one
cluster differently, and nothing notices. Pick one name per cluster and keep it. Surrounding
spaces are trimmed, so `" prod "` is `prod`; anything inside the name is checked as given.

**Where it shows today:** `submit` writes it into the Job, and the run prints it in phase 2.
The exporters label their series with it from M1-10, and the result artifact gets it as a
column with M1-14.

## Cleaning up

A run cleans up after itself, in phase 11. What it leaves depends on how it ended:

| The run | What stays | Until |
| --- | --- | --- |
| exited `0` | its Prometheus | you remove it |
| didn't exit `0` | its Prometheus, its owner and definition, and the pods of each test that was measured or never started | `dokimi cleanup` |
| stopped in its preflight | its Prometheus | you remove it |
| crashed, or its Job was stopped | whatever it had created | `dokimi cleanup`, once its claim has expired |

Its Job goes once `submit` or `attach` has fetched its reports, or an hour after it ended.
`submit --retain always` keeps a passing run's evidence too, and `--retain never` keeps
none. A test pod that may still be sending, such as the client of a test that timed out,
is never kept: the next run would measure next to it.

### dokimi cleanup

```console
$ dokimi cleanup 20260924-200229-ca532a
run 20260924-200229-ca532a: removed 4 object(s)
  ConfigMap/run-20260924-200229-ca532a-definition
  Pod/run-20260924-200229-ca532a-pair-001-client
  Pod/run-20260924-200229-ca532a-pair-001-server
  ConfigMap/run-20260924-200229-ca532a-owner
  kept: ConfigMap/run-20260924-200229-ca532a-prometheus, Service/run-20260924-200229-ca532a-prometheus, Deployment/run-20260924-200229-ca532a-prometheus, the run's Prometheus and its history. Add --prometheus to remove it too
```

Removes everything a run created, found by its labels, without the run's own process or
anything it remembered: so it also removes what a crashed run left, its node-exporter
included. The test pods go first, and it waits until they are gone, because a run's
client may still be sending; only then does the rest go, the owner last. The run's Job
goes with its owner, through the cluster's garbage collector, and a Deployment's or
DaemonSet's pods with them. `--older-than 24h` does the same for every run that started
more than 24 hours ago.

| Flag | |
| --- | --- |
| `--older-than <age>` | every run that started more than this long ago, instead of one run: `90m`, `24h` |
| `--prometheus` | remove the run's Prometheus too, and the history it holds |
| `--force` | remove a run that still holds the cluster; only for one whose Job is gone |
| `--dry-run` | list what would be removed, and remove nothing |
| `--namespace`, `-n` | the test type's namespace, if not `dokimi-iperf2` |
| `--kubeconfig`, `--context` | pick cluster credentials |

Two runs are refused, because removing them now would take something from a running one:

- **a run that still holds the cluster**, since its Job may still be running it. `--force`
  removes it anyway, for a run whose Job is gone;
- **a run that left a node-exporter, while another run holds the cluster.** A later run
  reuses a node-exporter a crashed run left, and it goes with that run's owner. Remove the
  run once the other has ended.

With `--older-than`, such a run is skipped and said so, and a closing line counts the runs
skipped. It exits `0` once nothing is left that it was asked to remove, `4` when the run
named by its id was refused, and `5` when something could not be removed: a refused
permission, a call that timed out, or a test pod that didn't go. It then lists what, and
the exact command to retry, with the flags you gave.

### The Prometheus

**Every run leaves its Prometheus behind**, on purpose: the run's history outlives the
run. It has no owner reference, so removing the run's owner leaves it alone, and `dokimi
cleanup` removes it only with `--prometheus`. Phase 4 prints the command that removes one
run's Prometheus, and the one that reaches it from your machine, [`dokimi
prometheus`](#prometheus):

```console
$ dokimi prometheus 20260924-200229-ca532a --namespace dokimi-iperf2
```

To remove every kept Prometheus at once:

```console
$ kubectl delete deployment,service,configmap -n dokimi-iperf2 \
    -l app.kubernetes.io/managed-by=dokimi,app.kubernetes.io/component=prometheus
```
