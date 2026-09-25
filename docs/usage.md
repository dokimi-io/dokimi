# Usage

The CLI is phase 1 of a run and the only part that executes outside the cluster. It
validates the definition, renders the manifests, submits the Job and streams its logs
back. Everything after that happens in the cluster (ADR-01-06).

```console
$ test-orchestrator validate test.yaml          # parse and check, no cluster involved
$ test-orchestrator submit test.yaml --dry-run  # render the matrix and the manifests
$ test-orchestrator submit test.yaml            # create the run and stream its logs
$ test-orchestrator attach <run-id>             # resume streaming a run already going
```

- [What a run does today](#what-a-run-does-today)
- [validate](#validate)
- [submit](#submit)
- [attach](#attach)
- [image](#image)
- [Exit codes](#exit-codes)
- [What a submission creates](#what-a-submission-creates)
- [Choosing the orchestrator image](#choosing-the-orchestrator-image)
- [Cleaning up](#cleaning-up)

## What a run does today

Milestone-01 is not finished. A submitted run parses its definition and stops:

```
run 20260924-200229-ca532a
phase 2 (ingestion): definition parsed
...
phases 3 to 11 are not implemented in this build: planning (#41), preflight (#42),
execution (#46), validation (#48), results (#49), cleanup and outcome (#51).
The run stops here without a verdict.
```

It exits `5`, not `0`. A run that stops early must not look like a run that passed.

Everything else on this page — validation, dry-run rendering, submission, streaming,
reattachment, exit-code propagation — works now.

## validate

```console
$ test-orchestrator validate examples/worker-01-to-worker-02-udp.yaml
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
test-orchestrator: line 26: unrecognised keyword 'Packet Loss Shuld Be'. A test case may
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

## submit

```console
$ test-orchestrator submit test.yaml
submitted run 20260924-200229-ca532a to namespace test-orchestrator-iperf2
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
| `--orchestrator-image` | override the image the Job runs |
| `--service-account` | run the Job as a specific service account |
| `--kubeconfig`, `--context` | pick cluster credentials |

### --dry-run

```console
$ test-orchestrator submit test.yaml --dry-run > manifests.yaml
Planned matrix (1 test)

  worker-01 -> worker-02
    type       iperf2 over udp
    duration   30s
    bandwidth  1G

Run id     20260924-200229-ca532a
Namespace  test-orchestrator-iperf2
Image      ghcr.io/test-orchestrator/orchestrator@sha256:...
Deadline   930s

dry run: nothing was created.
```

The manifests go to **stdout** and everything a person reads goes to **stderr**, so the
rendering can be saved or diffed on its own, as above.

A dry run is fully local. It renders from the same function `submit` uses, so the two
cannot drift, but it never contacts a cluster — not even to read.

The rendered `ownerReferences` carry an empty `uid`, which is why the output is not
directly appliable with `kubectl`. The API server assigns that uid when the owner object
is created, and submission fills it in before creating anything that points at it.

### --no-follow

Submits and returns immediately, printing how to pick the run back up:

```console
$ test-orchestrator submit test.yaml --no-follow
submitted run 20260924-200229-ca532a to namespace test-orchestrator-iperf2
not following. Reattach with: test-orchestrator attach 20260924-200229-ca532a
```

The exit code is `0` for a successful submission — it says nothing about the run, which
has not finished. Use `attach` to get the verdict.

### Interrupting

Ctrl-C stops the log stream, not the run:

```console
^C
stopped following run 20260924-200229-ca532a. The run is unaffected and continues in the
cluster; resume with: test-orchestrator attach 20260924-200229-ca532a
```

This is not special handling so much as the absence of it. The Job is a server-side
Kubernetes resource; nothing about it is tied to the client's connection. Killing the CLI
outright has the same effect.

## attach

```console
$ test-orchestrator attach 20260924-200229-ca532a
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
$ test-orchestrator image
ghcr.io/test-orchestrator/orchestrator@sha256:ab...
  source: release-default
  digest: sha256:ab...
```

Shows which image a run would submit and where that reference came from. See
[Choosing the orchestrator image](#choosing-the-orchestrator-image).

## Exit codes

Automation-facing from the first release, so they are an interface rather than an
implementation detail (ADR-01-08).

| Code | Meaning |
| --- | --- |
| `0` | run completed, overall pass |
| `1` | run completed, at least one non-optional pair failed |
| `2` | at least one non-optional pair errored, or a post-run anomaly |
| `3` | invalid request — a bad definition, run id or flag — detected before anything was created |
| `4` | run-level preflight failure |
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
them for the same classes of problem it finds there. Either way the code is what CI sees,
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

## What a submission creates

Three objects, in this order:

| Object | Purpose |
| --- | --- |
| `run-<id>-owner` (ConfigMap) | the run's owner; everything else points at it |
| `run-<id>-definition` (ConfigMap) | the authored document, mounted into the Job |
| `run-<id>` (Job) | the run itself |

They live in a namespace derived from the test type — `test-orchestrator-iperf2` — shared
by every run of that type rather than created per run, which would need cluster-level
permission on every invocation and make cleanup wait on namespace deletion (ADR-05-02).
The namespace is checked first and created only if it is not already there, so once it
exists, submitting needs only namespaced permissions.

If a later step of the submission fails — the Job is refused, say — the owner object is
deleted again, and takes with it anything already bound to it. That is different from a
failed *run*, whose objects are kept as evidence: a submission that never got as far as
a Job has no evidence to keep.

Every object carries `app.kubernetes.io/managed-by: test-orchestrator` and
`test-orchestrator/run-id: <id>`. Nothing without those labels is ever modified or
deleted, and every lookup and delete is scoped by label selector rather than by name.

The definition travels with the run in a ConfigMap rather than being re-read from your
disk. That is what makes a Job self-contained and lets `attach` need nothing local.

The Job carries `backoffLimit: 0`, `restartPolicy: Never` and an `activeDeadlineSeconds`
derived from the test duration plus a margin. The first two are requirements, not
preferences: the exit code is the verdict, so a controller that retried on non-zero would
re-run the whole matrix on a plain failure. The deadline is what stops an orphaned run on
its own — labels make cleanup possible, deadlines make it unnecessary.

Run ids are `YYYYMMDD-HHMMSS-xxxxxx` — the UTC start time to the second, then six characters
of `[0-9a-z]` — for example `20260924-143012-k3f9x2` (ADR-02-08). Fixed width, so they sort
by start time in a log, a label and a CSV alike. Lowercase, digits and hyphens only, so a
run is spelled the same way in its labels, its object names, Prometheus and the result
artifact. `attach` refuses anything else, including a run id in capitals.

## Choosing the orchestrator image

Precedence, highest first:

| Source | How | Scope |
| --- | --- | --- |
| Run override | `--orchestrator-image ghcr.io/example/orchestrator@sha256:…` | one run |
| Installation override | `TEST_ORCHESTRATOR_IMAGE` | one installation |
| Release default | baked in at release time, by digest | the released build |

A released CLI carries the digest of the image built from its own commit, so the two can
never resolve different dependency versions (ADR-05-08). A source checkout has no default
at all — see [Installation](installation.md#the-orchestrator-image).

An override is used exactly as given, including a plain tag, since an air-gapped mirror
may be all that offers one. What a run will not do is claim a pin it does not have: the
run metadata records the reference, the digest if there is one, and where the reference
came from.

## Cleaning up

Automatic cleanup arrives with M1-16 (#51). Until then, deleting a run's owner object
takes the rest with it — the cluster's own garbage collector cascades through the
`ownerReferences`:

```console
$ kubectl delete configmap run-20260924-200229-ca532a-owner -n test-orchestrator-iperf2
```

Or by label, across everything a run created:

```console
$ kubectl delete job,configmap -n test-orchestrator-iperf2 \
    -l test-orchestrator/run-id=20260924-200229-ca532a
```

The run id reads the same in the object name and the label, so either command can be
built from the id `submit` printed.
