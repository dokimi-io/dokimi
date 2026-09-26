# Test environment

A multi-node Kubernetes cluster on one machine, for developing against and for the
Milestone-01 acceptance run (M1-17). One command provisions it, one verifies it, one tears it
down, and all of it runs the same way locally and in CI.

```console
$ test-env/install-tools.sh .test-env/bin    # the pinned tools, checksum-verified
$ test-env/test-env.sh up kind-kindnet       # one control plane, three workers
$ test-env/test-env.sh check                 # does it provide what a run needs?
$ export KUBECONFIG=.test-env/kubeconfig
$ test-env/test-env.sh down                  # and prove nothing was left behind
```

**What this environment can and cannot tell you.** Every node is a container on one host, so
traffic between "nodes" crosses a host bridge, bounded by the host's CPU and memory bandwidth
rather than by a NIC, a switch or a real CNI datapath. It validates **orchestration
correctness** — does a run deploy, measure, evaluate, report and clean up correctly — and
**not measurement realism**. Throughput, loss and jitter measured here say nothing about real
hardware, and a threshold that is meaningful on real hardware is not meaningful here. The
reasoning, and what follows from it, is
[ADR-05-11](https://github.com/Test-Orchestrator/test-orchestrator-adrs/blob/main/adrs/05-11-emulated-test-environment-validates-orchestration-not-measurement.md).

It needs Docker and bash, and runs on macOS, Linux and Windows through WSL2. Which of those
it has actually been tested on, and with which container runtime, is listed in
[supported platforms](../docs/installation.md#supported-platforms).

## Profiles

A profile is a tool and a CNI. Every profile has the same shape: one control plane, tainted so
test pods never land on it, and three schedulable workers, on Kubernetes v1.37.0.

| Profile | Tool | CNI | NetworkPolicy enforced |
| --- | --- | --- | --- |
| `kind-kindnet` | kind | kindnet, kind's own | yes |
| `kind-calico` | kind | Calico | yes |
| `kind-cilium` | kind | Cilium | yes |
| `kind-flannel` | kind | flannel | **no** |
| `k3d-flannel` | k3d | flannel, k3s's own | yes, through k3s's embedded controller |
| `k3d-calico` | k3d | Calico | yes |
| `k3d-cilium` | k3d | Cilium | yes |

The last column is measured, not assumed: `check` applies a deny-all policy and fails if the
result disagrees with the table. It matters because a NetworkPolicy is the realistic way to make
pod-to-pod connectivity *fail* on purpose, which preflight's CNI check (M1-07) needs to be seen
failing. On `kind-flannel` that failure cannot be produced this way.

Use `kind-kindnet` unless you need something specific. It is the fastest to provision and
enforces NetworkPolicy. The other profiles exist so that CNI-specific behaviour can be compared,
not because a run needs them.

## Commands

| | |
| --- | --- |
| `up PROFILE [--load-modules]` | Provision the cluster, install the CNI if the tool has not, wait until every node is Ready. `--load-modules` lets it load a kernel module the profile needs, with `sudo` — see below |
| `check` | Verify the cluster provides what a run needs — see below |
| `nodes` | Print the schedulable workers, one per line, for use in a test definition |
| `load IMAGE` | Side-load a locally built image into every node |
| `down` | Delete the cluster, then fail if any Docker container, volume or network labelled with it remains |
| `profiles` | List the profiles |

One environment is up at a time. `up` refuses while another is up, and `down` needs no
argument because it tears down whichever one is.

State lives in `.test-env/` at the repository root, which is git-ignored: the cluster's own
kubeconfig and the name of the active profile. **Your default kubeconfig is never touched and
your current context is never switched** — point at the cluster with
`export KUBECONFIG=.test-env/kubeconfig`.

## What it changes on your machine

Everything, listed, so nothing is a surprise:

| | Undone by |
| --- | --- |
| Docker containers, a network and volumes for the cluster | `down`, which fails if any of them remain |
| `.test-env/`: the cluster's kubeconfig and the active profile | `down` |
| Your default kubeconfig and current context | never touched |
| **Kernel modules** | see below — they stay loaded until reboot |
| **Host-wide kernel settings** a cluster component changes | see below — they stay until reboot |

**This script never changes your kernel unless you ask it to.** One profile needs a module that
not every host loads: `kind-flannel` needs `br_netfilter`. If it's missing, `up` stops before
creating anything and tells you. Then either load it yourself:

```console
$ sudo modprobe br_netfilter
```

or pass `--load-modules`, and `up` loads it for you. It says what it's about to do before it
runs `sudo`, so you see the prompt and why it appears.

**Every profile makes the kernel load modules anyway**, and not because this script asks it to.
When kube-proxy and a CNI install their packet-filtering rules, the kernel loads the matching
netfilter extensions by itself. CNIs add their tunnel drivers on top, and k3s loads
`br_netfilter` on its own initiative. Measured on a clean GitHub Ubuntu runner:

| Profile | Modules that appeared while it came up |
| --- | --- |
| `kind-kindnet` | 11 netfilter extensions — `xt_nat`, `xt_mark`, `ipt_REJECT`, `nft_ct` and others |
| `kind-calico`, `k3d-calico` | netfilter extensions, plus `ipip`, `tunnel4` and `ip_set` hashes |
| `kind-cilium`, `k3d-cilium` | netfilter extensions, plus `geneve`, and socket and TPROXY support |
| `kind-flannel` | netfilter extensions — and `br_netfilter`, if you passed `--load-modules` |
| `k3d-flannel` | netfilter extensions, **and `br_netfilter`, loaded by k3s itself** |

You don't have to take this table's word for your machine. `up` compares the host kernel's
modules before and after, and names every one that appeared, whoever loaded it:

```
test-env: note: while kind-kindnet came up, these kernel modules were loaded into the host kernel: ipt_REJECT ...
test-env:       This script did not load them; the cluster's components or the kernel did. They stay loaded until reboot.
```

**Every profile also changes host-wide kernel settings**, again not because this script asks it to.
The kubelet sets some sysctls when it starts, and so do kind's nodes and Cilium. These settings
aren't namespaced, so a node container that sets one changes it for the whole host, and it stays
changed after `down`. Measured on a clean GitHub Ubuntu runner:

| Setting | Ubuntu's value → after `up` | Changed by | What it means |
| --- | --- | --- | --- |
| `kernel.panic_on_oops` | `0` → `1` | every profile (the kubelet) | A kernel oops becomes a kernel panic... |
| `kernel.panic` | `-1` → `10` | every profile (the kubelet) | ...and a panic reboots the host after 10 seconds |
| `vm.overcommit_memory` | `0` → `1` | every profile (the kubelet) | The kernel grants every memory allocation instead of refusing obvious overcommits |
| `kernel.core_pattern` | a pipe to `systemd-coredump` → `core` | kind profiles | Crashing programs on the host write a `core` file into their working directory instead of going to `coredumpctl` |
| `fs.nr_open` | `1048576` → `1073741816` | kind profiles | A higher ceiling on open files per process |
| `kernel.unprivileged_bpf_disabled` | `2` → `1` | Cilium profiles | Unprivileged BPF stays disabled, and now can't be re-enabled until reboot |
| `kernel.timer_migration` | `1` → `0` | Cilium profiles | Timers stay on the CPU that set them |

**On a Linux machine you work on, the first two matter most:** until you reboot, an oops that the
machine would otherwise survive reboots it instead. They are the kubelet's defaults on any
Kubernetes node, and nothing in kind or k3d turns them off.

`up` says which of these it's about to change before it creates anything, so you can stop there.
Afterwards it reads every setting under `kernel.*`, `vm.*` and `fs.*` a second time, and names each
one that changed, with its old and new value:

```
test-env: note: while kind-kindnet came up, these host-wide kernel settings changed:
test-env:         kernel.panic: -1 -> 10
test-env:         kernel.panic_on_oops: 0 -> 1
test-env:         vm.overcommit_memory: 0 -> 1
test-env:       This script did not set them; the cluster's components did. They stay until reboot. To undo one: sudo sysctl KEY=OLD-VALUE
```

To undo one, set the old value it printed, for example `sudo sysctl kernel.panic_on_oops=0`.
`kernel.unprivileged_bpf_disabled` is the exception: once it is `1`, the kernel refuses any change
until reboot. Values that change by themselves aren't compared: open-file and inode counts,
disk-quota statistics, the random UUID and the last PID. Neither is `net.*`, because most of it
belongs to a network namespace, so each node gets its own copy and the host's copy isn't touched.

On a machine that has run Kubernetes since its last reboot, most of these are already loaded or
set, and `up` has nothing to report. That doesn't mean nothing was changed. It means the changes
were already there. On macOS, "the host kernel" is the Linux VM your Docker runs in, not macOS
itself.

## What `check` verifies

Each one comes from what Milestone-01 exercises (M1-18):

1. **Four nodes, all Ready**; exactly three schedulable workers; the control plane tainted
   `NoSchedule`. "Schedulable" uses the same rule the orchestrator's planning does: not cordoned,
   no `NoSchedule` or `NoExecute` taint.
2. **Pod-to-pod across distinct nodes, in every direction** — a probe pod on each worker, each
   fetching from the other two: six directed paths.
3. **Cluster DNS** resolves from inside a pod, and **a Service** is reachable by its DNS name from
   another node.
4. **Side-loading:** a locally built image runs with `imagePullPolicy: Never`, which is how an
   orchestrator image you built yourself gets into the cluster.
5. **Image-pull failure is reproducible:** an image that does not exist fails as `ErrImagePull`,
   because preflight treats it as its own signal.
6. **NetworkPolicy** is enforced or not, as the table above says, and traffic recovers when the
   policy is removed.

It ends by restating what the environment does not show. In CI it writes the same statement to
the job summary, where a reader of a green run will see it.

## Running a test against it

```console
$ test-env/test-env.sh up kind-kindnet
$ docker build -f images/orchestrator/Dockerfile -t orchestrator:dev .
$ test-env/test-env.sh load orchestrator:dev
$ kubectl apply -f deploy/rbac.yaml
$ test-env/test-env.sh nodes
test-orchestrator-worker
test-orchestrator-worker2
test-orchestrator-worker3
```

Put two of those names in the definition's `placement`, then submit:

```console
$ export KUBECONFIG=.test-env/kubeconfig TEST_ORCHESTRATOR_IMAGE=orchestrator:dev
$ uv run test-orchestrator submit test.yaml --dry-run --kubeconfig .test-env/kubeconfig
$ uv run test-orchestrator submit test.yaml --service-account test-orchestrator
```

The worker names differ by tool: kind calls them `test-orchestrator-worker`, `-worker2` and
`-worker3`, and k3d calls them `k3d-test-orchestrator-agent-0` to `-agent-2`. `nodes` prints
whichever this cluster has.

## Things that will catch you out

- **Side-load an image you built, not a public image you re-tagged.** Re-tagging a pulled
  multi-architecture image and loading it produces an image index whose other-platform layers
  are missing. containerd then refuses it with `ErrImageNeverPull` — even though the image is
  listed on the node. An image built locally is single-platform and loads fine.
- **k3s makes its server schedulable by default.** The k3d configs taint it, so both tools give
  exactly three places a test pod can go. They also turn off Traefik, the service load balancer
  and metrics-server, which kind does not ship either.
- **flannel on kind needs one more CNI plugin.** flannel hands pod networking to the `bridge`
  plugin, which kind's node image does not ship. Without it every pod fails with "failed to
  delegate add: failed to find plugin" and CoreDNS never starts, although every node reports
  Ready. `up kind-flannel` adds that one binary from the pinned reference plugins.
- **flannel on kind needs the `br_netfilter` kernel module**, which not every Linux host loads —
  GitHub's Ubuntu runners don't, OrbStack does. Without it every flannel pod crash-loops with
  "Failed to check br_netfilter". `up kind-flannel` checks first; see
  [what it changes on your machine](#what-it-changes-on-your-machine) for the two ways to provide
  it. To keep it across reboots:
  `echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf`.
- **Cilium on k3d needs to be told where the API server is.** cilium-cli takes the address from
  the kubeconfig. k3d's kubeconfig points at `0.0.0.0` and a host-mapped port, which means
  nothing inside the cluster, so the agents retry forever and stay Pending. `up k3d-cilium`
  points them at the server container instead. kind needs no help, because cilium-cli detects it.
- **macOS ships bash 3.2.** The scripts are written for it, so no Homebrew bash is needed. One
  consequence worth knowing if you edit them: under `set -u`, bash 3.2 treats an empty array as
  unbound, so `"${a[@]}"` has to be written `${a[@]+"${a[@]}"}`. A test enforces it.

## Versions

Every version, image digest and manifest checksum is in [`versions.env`](versions.env), and
nowhere else. `tests/test_test_env.py` holds the cluster configs, the scripts and the CI
workflow to it. `install-tools.sh` checks each binary against the checksum published with its
release. That catches a corrupted download, but not a compromised release.

| | |
| --- | --- |
| kind | v0.33.0, node image `kindest/node:v1.37.0` by digest |
| k3d | v5.9.0, `rancher/k3s:v1.37.0-k3s1` by digest |
| Calico | v3.32.2, manifest by checksum |
| Cilium | 1.20.2, through cilium-cli v0.20.1 |
| flannel | v0.28.9, manifest by checksum, plus the `bridge` plugin from containernetworking/plugins v1.9.1 on kind |
| kubectl | v1.37.0 |

## Not here yet

A profile on **virtual machines** — separate kernels, and the only option where traffic between
nodes resembles a real path — is deliberately deferred to the first milestone that needs
measurements to mean something (ADR-05-11). Until then, the answer to "are these numbers real?"
is no, and this page says so.
