# Examples on a local kind cluster

Three tests and one broken cluster, on the repository's [test environment](../../test-env/README.md).
They show what a run can tell you: a pass, an SLA in Robot Framework, a failure and how to find
its cause in the run's own metrics, and a cluster that fails its preflight.

**The numbers mean nothing about a real network.** Every kind node is a container on your
machine, so traffic between them is bounded by your CPU, not by a NIC or a switch. The thresholds
here are chosen so the verdicts come out the same on most machines. See
[what this cluster can and cannot tell you](../../docs/setup.md#a-cluster-to-submit-runs-against).

| File | What it shows | Verdict | Exit |
| --- | --- | --- | --- |
| [`01-tcp-baseline.yaml`](01-tcp-baseline.yaml) | TCP between two nodes, at least 1 Gbit/s | pass | `0` |
| [`02-udp-voice-sla.robot`](02-udp-voice-sla.robot) | 50 Mbit/s of UDP with loss ≤ 0.1 % and jitter ≤ 1 ms, written in Robot Framework | pass | `0` |
| [`03-udp-beyond-one-flow.yaml`](03-udp-beyond-one-flow.yaml) | 10 Gbit/s of UDP in one flow, more than one host can carry | fail | `1` |
| A NetworkPolicy, below | The test's servers isolated: the preflight finds it before measuring | cluster not ready | `4` |

## Setting up

From the repository's root, once:

```console
$ test-env/install-tools.sh .test-env/bin
$ export PATH="$PWD/.test-env/bin:$PATH"
$ test-env/test-env.sh up kind-kindnet
$ out=$(test-env/test-env.sh images) && eval "$out"
$ export KUBECONFIG=.test-env/kubeconfig
$ kubectl apply -f deploy/rbac.yaml
```

`images` builds the orchestrator and `iperf2` images from your checkout and loads them into the
nodes. With a released CLI (`uv tool install dokimi-io`), leave it out, and set
`DOKIMI_CLUSTER=test-env` yourself: the release uses its own published images. The examples name
`dokimi-worker`, `dokimi-worker2` and `dokimi-worker3`, the workers every kind profile has.

Each run takes a little over a minute, preflight and warm-up included. Its reports come back to
`./results/<run-id>/`.

## 1. A baseline

```console
$ uv run dokimi submit examples/kind/01-tcp-baseline.yaml --service-account dokimi
$ echo $?
0
```

The verdict is the exit code, so a CI job needs nothing else. `results/<run-id>/assertions.csv`
holds every criterion with its measured value, one line each.

## 2. An SLA, in Robot Framework

```console
$ uv run dokimi submit examples/kind/02-udp-voice-sla.robot --service-account dokimi
$ uv run dokimi convert --to yaml examples/kind/02-udp-voice-sla.robot
```

Three criteria, one for each of the measures a UDP test has: throughput, packet loss and
jitter. `convert` prints the same test as YAML: the two formats are equal peers.

## 3. A failure, and its cause

```console
$ uv run dokimi submit examples/kind/03-udp-beyond-one-flow.yaml --service-account dokimi
$ echo $?
1
```

It asks one UDP flow for 10 Gbit/s, and gets much less, with heavy loss. On one Mac with
OrbStack (10 CPUs) it measured 1.97 Gbit/s received and 45 % loss. The run kept its
Prometheus, so open it:

```console
$ uv run dokimi prometheus <run-id>
run <run-id>'s Prometheus: http://127.0.0.1:54817
  the run started …, ended …
```

Open the URL, choose **Graph**, and set the end of the range to the time it printed. These
show where the traffic was lost:

| What | PromQL | On that Mac |
| --- | --- | --- |
| What the sender sent | `dokimi_throughput_bits_per_second{direction="outbound"} / 1e6` | 3,575 Mbit/s |
| What reached the receiving node | `rate(node_network_receive_bytes_total{device="eth0"}[15s]) * 8 / 1e6` | 3,684 Mbit/s on `dokimi-worker3` |
| What the receiver got | `dokimi_throughput_bits_per_second{direction="inbound"} / 1e6` | 1,975 Mbit/s |
| CPU, busiest core per node | `max by (node) (1 - rate(node_cpu_seconds_total{mode="idle"}[15s]))` | 0.66–0.72 |

Everything the sender sent arrived at the receiving node; the loss is in the receiving host.
On kind, the CPU row is almost the same for every node: they share one kernel, so each node's
counters are the whole machine's. On real nodes the same query shows each node's own cores,
and a single saturated core is often the limit of one flow.

## 4. A cluster that isn't ready

A NetworkPolicy that isolates the test's servers, as a cluster with a default-deny policy
might:

```console
$ kubectl apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: isolate-iperf2-servers
  namespace: dokimi-iperf2
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: iperf2
      app.kubernetes.io/component: server
  policyTypes: [Ingress]
EOF
$ uv run dokimi preflight examples/kind/01-tcp-baseline.yaml --service-account dokimi
dokimi: cluster not ready (connectivity): the probe of pair-001 (dokimi-worker -> dokimi-worker2): no TCP to the server pod at 10.244.1.11:5001 arrived
$ echo $?
4
$ kubectl delete networkpolicy -n dokimi-iperf2 isolate-iperf2-servers
```

`preflight` runs only the checks, with the test's own pods, and measures nothing. `submit`
runs the same checks first, so a real run stops in the same place. `kind-flannel` enforces no
NetworkPolicy, so there the probe passes.

## Cleaning up

A run that didn't pass keeps its pods for you to look at, and every run keeps its Prometheus.

```console
$ uv run dokimi cleanup --older-than 1m --prometheus
$ test-env/test-env.sh down
```
