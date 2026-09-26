#!/usr/bin/env bash
# The multi-node test environment (M1-18): provision, check, tear down.
#
#   test-env/test-env.sh profiles           list the profiles
#   test-env/test-env.sh up PROFILE         provision a cluster: one control plane, three workers
#                        [--load-modules]  and load a kernel module the profile needs, with sudo
#   test-env/test-env.sh check              verify it provides what a run needs
#   test-env/test-env.sh nodes              print the schedulable worker names
#   test-env/test-env.sh load IMAGE         side-load a locally built image into every node
#   test-env/test-env.sh down               delete it and verify nothing was left behind
#
# A profile is a tool and a CNI: kind-kindnet, kind-calico, kind-cilium, kind-flannel,
# k3d-flannel, k3d-calico, k3d-cilium. Every version is pinned in versions.env.
#
# State lives in .test-env/ at the repository root: the cluster's own kubeconfig and the
# active profile. Your default kubeconfig is never touched; point at the cluster with
#   export KUBECONFIG=.test-env/kubeconfig
#
# Clusters from this environment validate orchestration correctness, not measurement
# realism: every node shares one host's network stack. See test-env/README.md.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
# shellcheck source=test-env/versions.env
source "$here/versions.env"

state="${TEST_ENV_STATE:-$repo/.test-env}"
cluster="${TEST_ENV_CLUSTER:-dokimi}"
[[ -d "$state/bin" ]] && export PATH="$state/bin:$PATH"
export KUBECONFIG="$state/kubeconfig"

PROFILES=(kind-kindnet kind-calico kind-cilium kind-flannel k3d-flannel k3d-calico k3d-cilium)
WORKERS=3
CHECK_NAMESPACE=test-env-check

# Whether each profile's CNI enforces NetworkPolicy. `check` measures it and fails on a
# mismatch, so this is a tested fact about each profile rather than a belief.
# (A function rather than an associative array: macOS still ships bash 3.2.)
enforces_network_policy() {
  case "$1" in
    kind-flannel) echo no ;;
    *) echo yes ;;
  esac
}

# -- helpers -------------------------------------------------------------------------------

die() { echo "test-env: $*" >&2; exit 1; }
say() { echo "test-env: $*"; }

tool_of() { echo "${1%%-*}"; }
cni_of() { echo "${1#*-}"; }

require_profile() {
  local profile="$1" known
  for known in "${PROFILES[@]}"; do [[ "$known" == "$profile" ]] && return 0; done
  die "unknown profile '$profile'. Profiles: ${PROFILES[*]}"
}

active_profile() {
  [[ -f "$state/profile" ]] || die "no environment is up. Start one with: test-env/test-env.sh up PROFILE"
  cat "$state/profile"
}

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null || die "'$tool' is not installed. test-env/install-tools.sh installs the pinned versions into .test-env/bin"
  done
}

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch_verified() { # fetch_verified URL FILE EXPECTED_SHA256
  curl -fsSL --retry 3 -o "$2" "$1"
  local actual; actual="$(sha256 "$2")"
  [[ "$actual" == "$3" ]] || die "checksum mismatch for $1: expected $3, got $actual"
}

pod_cidr() { # the pod network each tool assigns, which a manually installed CNI must match
  case "$(tool_of "$1")" in kind) echo 10.244.0.0/16 ;; k3d) echo 10.42.0.0/16 ;; esac
}

node_containers() { # the Docker containers that are this cluster's nodes
  case "$(tool_of "$1")" in
    kind) docker ps -q --filter "label=io.x-k8s.kind.cluster=$cluster" ;;
    k3d) docker ps -q --filter "label=k3d.cluster=$cluster" --filter "label=k3d.role=server" \
           && docker ps -q --filter "label=k3d.cluster=$cluster" --filter "label=k3d.role=agent" ;;
  esac
}

# -- up ------------------------------------------------------------------------------------

cmd_up() {
  local profile="" load_modules=no arg
  for arg in "$@"; do
    case "$arg" in
      --load-modules) load_modules=yes ;;
      -*) die "unknown option '$arg'. usage: test-env.sh up PROFILE [--load-modules]" ;;
      *) [[ -z "$profile" ]] || die "one profile at a time"; profile="$arg" ;;
    esac
  done
  [[ -n "$profile" ]] || die "usage: test-env.sh up PROFILE [--load-modules]. Profiles: ${PROFILES[*]}"
  require_profile "$profile"
  [[ -f "$state/profile" ]] && die "an environment is already up ($(cat "$state/profile")). Tear it down first: test-env/test-env.sh down"

  local tool cni config
  tool="$(tool_of "$profile")"
  cni="$(cni_of "$profile")"
  require_tools docker kubectl "$tool"
  [[ "$cni" == cilium ]] && require_tools cilium
  mkdir -p "$state"

  case "$profile" in kind-kindnet | k3d-flannel) config="$here/$tool/cluster.yaml" ;; *) config="$here/$tool/cluster-no-cni.yaml" ;; esac
  [[ "$profile" == kind-flannel ]] && require_br_netfilter "$load_modules"
  announce_kernel_changes "$profile"
  local modules_before settings_before
  modules_before="$(kernel_modules)"
  settings_before="$(kernel_settings)"

  # Recorded before anything is created, not after. Whatever fails from here on, `down`
  # knows which cluster to remove: written after creation, a failure inside `kind create`
  # or `k3d cluster create` would leave a cluster that `down` refused to touch.
  echo "$profile" > "$state/profile"
  trap 'on_up_failure "$profile"' EXIT

  say "creating $profile: 1 control plane + $WORKERS workers, Kubernetes from versions.env"
  case "$tool" in
    kind)
      kind create cluster --name "$cluster" --image "$KIND_NODE_IMAGE" --config "$config" \
        --kubeconfig "$KUBECONFIG"
      ;;
    k3d)
      k3d cluster create "$cluster" --config "$config" --image "$K3S_IMAGE" \
        --kubeconfig-update-default=false --kubeconfig-switch-context=false --wait
      k3d kubeconfig get "$cluster" > "$KUBECONFIG"
      ;;
  esac

  install_cni "$profile"

  say "waiting for every node to be Ready"
  kubectl wait --for=condition=Ready node --all --timeout=300s >/dev/null
  kubectl -n kube-system rollout status deployment/coredns --timeout=300s >/dev/null
  report_kernel_changes "$modules_before" "$settings_before" "$profile"
  trap - EXIT
  say "$profile is up. export KUBECONFIG=$KUBECONFIG"
}

on_up_failure() {
  # Runs when `up` exits early. The partial cluster is kept rather than deleted, because
  # what it contains is the evidence of why it failed; CI's failure step reads it.
  local rc=$?
  [[ "$rc" -eq 0 ]] && return
  echo "test-env: bringing up $1 failed. Whatever was created is kept for inspection" \
    "(export KUBECONFIG=$KUBECONFIG); remove it with: test-env/test-env.sh down" >&2
}

install_cni() {
  local profile="$1" cidr
  cidr="$(pod_cidr "$profile")"
  case "$profile" in
    kind-kindnet | k3d-flannel)
      say "using $(cni_of "$profile"), the tool's own CNI"
      ;;
    *-calico)
      say "installing Calico $CALICO_VERSION for pod network $cidr"
      fetch_verified "https://raw.githubusercontent.com/projectcalico/calico/$CALICO_VERSION/manifests/calico.yaml" \
        "$state/calico.yaml" "$CALICO_MANIFEST_SHA256"
      # The manifest ships its pool CIDR commented out. Calico would otherwise pick its own
      # default, which the cluster's pod network does not route.
      sed -i.orig \
        -e "s|# - name: CALICO_IPV4POOL_CIDR|- name: CALICO_IPV4POOL_CIDR|" \
        -e "s|#   value: \"192.168.0.0/16\"|  value: \"$cidr\"|" \
        "$state/calico.yaml"
      kubectl apply -f "$state/calico.yaml" >/dev/null
      kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s >/dev/null
      kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=300s >/dev/null
      ;;
    *-cilium)
      if [[ "$(tool_of "$profile")" == k3d ]]; then
        # k3s nodes do not mount the BPF filesystem or a cgroup2 hierarchy for Cilium the
        # way kind's nodes do, so each node container gets them before the agent starts.
        local node
        for node in $(node_containers "$profile"); do
          docker exec "$node" sh -c 'mount bpffs /sys/fs/bpf -t bpf && mount --make-shared /sys/fs/bpf
            mkdir -p /run/cilium/cgroupv2 && mount -t cgroup2 none /run/cilium/cgroupv2 && mount --make-shared /run/cilium/cgroupv2'
        done
      fi
      # `${extra[@]+...}` rather than "${extra[@]}": bash 3.2 treats an empty array as unbound
      # under `set -u`, which would break kind-cilium, where extra stays empty.
      local -a extra=()
      if [[ "$(tool_of "$profile")" == k3d ]]; then
        # cilium-cli takes the API server address from the kubeconfig. k3d's kubeconfig
        # points at 0.0.0.0 and a host-mapped port, which means nothing inside the
        # cluster, so the agent would retry forever. The server container's own name
        # resolves on k3d's Docker network. kind needs no help: cilium-cli detects it.
        extra=(--set "k8sServiceHost=k3d-$cluster-server-0" --set k8sServicePort=6443)
      fi
      say "installing Cilium $CILIUM_VERSION for pod network $cidr"
      cilium install --version "$CILIUM_VERSION" --set ipam.mode=kubernetes ${extra[@]+"${extra[@]}"} >/dev/null
      cilium status --wait --wait-duration 5m >/dev/null
      ;;
    kind-flannel)
      add_bridge_plugin "$profile"
      say "installing flannel $FLANNEL_VERSION for pod network $cidr"
      fetch_verified "https://github.com/flannel-io/flannel/releases/download/$FLANNEL_VERSION/kube-flannel.yml" \
        "$state/kube-flannel.yml" "$FLANNEL_MANIFEST_SHA256"
      kubectl apply -f "$state/kube-flannel.yml" >/dev/null
      kubectl -n kube-flannel rollout status daemonset/kube-flannel-ds --timeout=300s >/dev/null
      ;;
  esac
}

require_br_netfilter() { # require_br_netfilter LOAD(yes|no)
  # flannel refuses to start without the br_netfilter kernel module ("Failed to check
  # br_netfilter"), crash-looping on every node. Not every host loads it: GitHub's Ubuntu
  # runners do not, OrbStack's kernel does. A container shares the host kernel, and the
  # module's settings appear in every network namespace once it is loaded, so a
  # throwaway container can tell before any cluster exists.
  #
  # Loading a kernel module changes the host, so it never happens unasked: without
  # --load-modules this stops and says what to run; with it, it says what it is about to
  # do, then does it with sudo, where you can see it.
  has_br_netfilter && return 0
  if [[ "$1" != yes ]]; then
    die "kind-flannel needs the br_netfilter kernel module, which this host has not loaded. Either load it yourself: sudo modprobe br_netfilter (to keep it across reboots: echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf), or rerun with --load-modules to have this script run that modprobe for you. Every other profile works without it."
  fi
  say "loading the br_netfilter kernel module into this host's kernel, with sudo, because you passed --load-modules. It stays loaded until reboot, or until: sudo modprobe -r br_netfilter"
  sudo modprobe br_netfilter || die "could not load br_netfilter. Load it yourself (sudo modprobe br_netfilter) and rerun without --load-modules"
  has_br_netfilter || die "br_netfilter was loaded but its settings are not visible to containers; this host's Docker cannot run kind-flannel"
}

has_br_netfilter() {
  docker run --rm "$CHECK_IMAGE" test -e /proc/sys/net/bridge/bridge-nf-call-iptables >/dev/null 2>&1
}

kernel_modules() {
  # The host kernel's loaded modules, as a container sees them: /proc/modules is not
  # namespaced, so this is the kernel the cluster's nodes share - the Linux VM, on macOS.
  docker run --rm "$CHECK_IMAGE" cut -d' ' -f1 /proc/modules 2>/dev/null | sort
}

kernel_settings() {
  # The host-wide kernel settings under /proc/sys, as `path=value`. Only kernel/, vm/ and
  # fs/: net/ is per network namespace, so a container sees its own values there, not the
  # host's. Left out too are values that change by themselves - counters, a random UUID,
  # the last PID, disk-quota statistics - and the container's own hostname. Found by
  # snapshotting twice with nothing in between, and by what CI's clean runners reported.
  docker run --rm "$CHECK_IMAGE" sh -c '
    cd /proc/sys && find kernel vm fs -type f | sort | while read -r f; do
      v="$(tr "\n\t" "  " < "$f" | sed "s/ *\$//")" && printf "%s=%s\n" "$f" "$v"
    done' 2>/dev/null \
    | grep -v -E '^(fs/(dentry-state|file-nr|inode-nr|inode-state|aio-nr|quota/(lookups|drops|reads|writes|cache_hits|allocated_dquots|free_dquots|syncs))|kernel/(hostname|domainname|ns_last_pid|random/uuid|random/entropy_avail|pty/nr))='
}

announce_kernel_changes() { # announce_kernel_changes PROFILE
  # What CI measured each profile changing on a clean host, said before anything is
  # created. The kubelet and the CNIs change these through privileged node containers,
  # without sudo and without asking, so the only way to tell the user first is to say it
  # here. test-env/README.md has the same list as a table; a test keeps the two in step.
  say "note: bringing up $1 changes these host-wide kernel settings, until reboot:"
  say "        kernel.panic_on_oops=1 and kernel.panic=10, by the kubelet: a kernel oops reboots the host after 10 seconds"
  say "        vm.overcommit_memory=1, by the kubelet"
  if [[ "$(tool_of "$1")" == kind ]]; then
    say "        kernel.core_pattern=core and fs.nr_open=1073741816, by kind's nodes"
  fi
  if [[ "$(cni_of "$1")" == cilium ]]; then
    say "        kernel.unprivileged_bpf_disabled=1 and kernel.timer_migration=0, by Cilium"
  fi
  say "      On macOS that is Docker's Linux VM, not macOS. Once $1 is up, what actually changed is listed."
}

report_kernel_changes() { # report_kernel_changes MODULES_BEFORE SETTINGS_BEFORE PROFILE
  # This script changes the kernel only when asked. The tools it drives are not so
  # careful: k3s, a CNI, the kubelet, or the kernel's own auto-loading can load modules and
  # change host-wide settings while a cluster comes up. Nothing changes the host kernel
  # without the user being told, so whatever changed during provisioning is reported,
  # whoever changed it.
  local added changed line key old
  added="$(comm -13 <(echo "$1") <(kernel_modules) | tr '\n' ' ')"
  if [[ -n "${added// /}" ]]; then
    say "note: while $3 came up, these kernel modules were loaded into the host kernel: $added"
    say "      This script did not load them; the cluster's components or the kernel did. They stay loaded until reboot."
  fi
  changed="$(comm -13 <(echo "$2") <(kernel_settings))"
  if [[ -n "$changed" ]]; then
    say "note: while $3 came up, these host-wide kernel settings changed:"
    while IFS= read -r line; do
      key="${line%%=*}"
      old="$(echo "$2" | grep "^$key=" | cut -d= -f2- || true)"
      say "        ${key//\//.}: ${old:-(not present)} -> ${line#*=}"
    done <<< "$changed"
    say "      This script did not set them; the cluster's components did. They stay until reboot. To undo one: sudo sysctl KEY=OLD-VALUE"
    say "      (kernel.unprivileged_bpf_disabled=1 is the exception: the kernel refuses to change it again until reboot)."
  fi
}

add_bridge_plugin() {
  # flannel delegates pod setup to the `bridge` CNI plugin, which kind's node image does
  # not include: without it every pod fails with "failed to delegate add: failed to find
  # plugin", and CoreDNS never starts. Only `bridge` is added, so none of the plugins the
  # node image ships are replaced.
  local profile="$1" arch expected node
  case "$(docker info --format '{{.Architecture}}')" in
    x86_64 | amd64) arch=amd64; expected="$CNI_PLUGINS_SHA256_AMD64" ;;
    aarch64 | arm64) arch=arm64; expected="$CNI_PLUGINS_SHA256_ARM64" ;;
    *) die "no CNI plugins pinned for node architecture $(docker info --format '{{.Architecture}}')" ;;
  esac
  say "adding the bridge CNI plugin $CNI_PLUGINS_VERSION ($arch) to every node"
  fetch_verified "https://github.com/containernetworking/plugins/releases/download/$CNI_PLUGINS_VERSION/cni-plugins-linux-$arch-$CNI_PLUGINS_VERSION.tgz" \
    "$state/cni-plugins.tgz" "$expected"
  tar -xzf "$state/cni-plugins.tgz" -C "$state" ./bridge
  for node in $(node_containers "$profile"); do docker cp "$state/bridge" "$node:/opt/cni/bin/bridge"; done
  rm -f "$state/cni-plugins.tgz" "$state/bridge"
}

# -- nodes, load ---------------------------------------------------------------------------

schedulable_workers() {
  # A node a test pod can land on: not cordoned, no NoSchedule or NoExecute taint. The
  # same rule planning applies (src/dokimi/planning/nodes.py).
  kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.unschedulable}{"\t"}{.spec.taints[*].effect}{"\n"}{end}' \
    | awk -F'\t' '$2 != "true" && $3 !~ /NoSchedule|NoExecute/ {print $1}' | sort
}

cmd_nodes() { active_profile >/dev/null; schedulable_workers; }

cmd_load() {
  local image="${1:-}" profile
  [[ -n "$image" ]] || die "usage: test-env.sh load IMAGE"
  profile="$(active_profile)"
  case "$(tool_of "$profile")" in
    kind) kind load docker-image "$image" --name "$cluster" >/dev/null ;;
    k3d) k3d image import "$image" --cluster "$cluster" >/dev/null ;;
  esac
  say "loaded $image into every node of $profile"
}

# -- check ---------------------------------------------------------------------------------

failures=0
ok() { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

probe_manifest() { # probe_manifest NAME NODE
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: $CHECK_NAMESPACE
  labels: {app: probe, probe: $1}
spec:
  nodeName: $2
  terminationGracePeriodSeconds: 0
  containers:
    - name: probe
      image: $CHECK_IMAGE
      command: [sh, -c, 'mkdir -p /www && echo "$1" > /www/index.html && exec httpd -f -p 8080 -h /www']
      ports: [{containerPort: 8080}]
YAML
}

fetch_from() { # fetch_from POD URL -> body, or nothing within 3 s
  kubectl -n "$CHECK_NAMESPACE" exec "$1" -- wget -q -T 3 -O - "$2" 2>/dev/null || true
}

cmd_check() {
  local profile total ready tainted i j worker
  local -a workers ip
  profile="$(active_profile)"
  require_tools docker kubectl
  say "checking $profile against what a run needs (M1-18)"

  # 1. Shape: one control plane nobody schedules onto, three workers everyone can.
  total="$(kubectl get nodes --no-headers | wc -l | tr -d ' ')"
  ready="$(kubectl get nodes --no-headers | awk '$2 == "Ready"' | wc -l | tr -d ' ')"
  workers=()
  while IFS= read -r worker; do workers+=("$worker"); done < <(schedulable_workers)
  tainted="$(kubectl get nodes -l node-role.kubernetes.io/control-plane -o jsonpath='{.items[*].spec.taints[*].effect}')"
  if [[ "$total" == $((WORKERS + 1)) && "$ready" == "$total" ]]; then ok "$total nodes, all Ready"; else bad "expected $((WORKERS + 1)) Ready nodes, found $ready of $total Ready"; fi
  if [[ "${#workers[@]}" == "$WORKERS" ]]; then ok "$WORKERS schedulable workers: ${workers[*]}"; else bad "expected $WORKERS schedulable workers, found ${#workers[@]}: ${workers[*]:-none}"; fi
  if [[ "$tainted" == *NoSchedule* ]]; then ok "control plane is tainted NoSchedule"; else bad "control plane is schedulable; test pods could land on it"; fi

  # Everything below needs pods on at least two distinct workers.
  if [[ "${#workers[@]}" -lt 2 ]]; then finish_check "$profile"; return; fi

  kubectl delete namespace "$CHECK_NAMESPACE" --ignore-not-found --wait >/dev/null
  kubectl create namespace "$CHECK_NAMESPACE" >/dev/null

  # 2. A probe pod on every worker, serving its own name.
  for i in "${!workers[@]}"; do probe_manifest "probe-$i" "${workers[$i]}" | kubectl apply -f - >/dev/null; done
  if ! kubectl -n "$CHECK_NAMESPACE" wait --for=condition=Ready pod -l app=probe --timeout=180s >/dev/null; then
    bad "probe pods did not become Ready"; finish_check "$profile"; return
  fi
  for i in "${!workers[@]}"; do ip[i]="$(kubectl -n "$CHECK_NAMESPACE" get pod "probe-$i" -o jsonpath='{.status.podIP}')"; done

  # 3. Pod-to-pod across distinct nodes, every direction.
  local paths=0 working=0
  for i in "${!workers[@]}"; do
    for j in "${!workers[@]}"; do
      [[ "$i" == "$j" ]] && continue
      paths=$((paths + 1))
      if [[ "$(fetch_from "probe-$i" "http://${ip[$j]}:8080")" == "probe-$j" ]]; then working=$((working + 1)); else bad "no pod-to-pod path ${workers[$i]} -> ${workers[$j]}"; fi
    done
  done
  if [[ "$working" == "$paths" ]]; then ok "pod-to-pod across nodes: all $paths directed paths"; fi

  # 4. Cluster DNS and Service routing, from inside a pod.
  kubectl -n "$CHECK_NAMESPACE" expose pod probe-0 --name probe-service --port 8080 >/dev/null
  if kubectl -n "$CHECK_NAMESPACE" exec probe-1 -- nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1; then ok "cluster DNS resolves from a pod"; else bad "cluster DNS does not resolve from a pod"; fi
  local served=""
  for _ in 1 2 3 4 5; do served="$(fetch_from probe-1 "http://probe-service.$CHECK_NAMESPACE.svc.cluster.local:8080")"; [[ "$served" == probe-0 ]] && break; sleep 2; done
  if [[ "$served" == probe-0 ]]; then ok "a Service is reachable by DNS name from another node"; else bad "a Service is not reachable by its DNS name"; fi

  # 5. Image side-loading, which is how a locally built orchestrator image gets in. The
  # probe is *built* here rather than a pulled image re-tagged: re-tagging a multi-arch
  # image exports an index whose other-platform blobs are missing, and containerd then
  # refuses it with ErrImageNeverPull even though the image is listed on the node. A
  # locally built image is single-platform, like the orchestrator image.
  printf 'FROM %s\nLABEL org.opencontainers.image.description="test-env side-load probe"\n' "$CHECK_IMAGE" \
    | docker build -q -t test-env/sideload-probe:check - >/dev/null
  cmd_load test-env/sideload-probe:check >/dev/null 2>&1
  kubectl -n "$CHECK_NAMESPACE" run sideload --image test-env/sideload-probe:check --image-pull-policy Never \
    --restart Never --overrides "{\"spec\":{\"nodeName\":\"${workers[0]}\"}}" --command -- sh -c 'echo loaded' >/dev/null
  if kubectl -n "$CHECK_NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded pod/sideload --timeout=90s >/dev/null 2>&1; then ok "a side-loaded image runs without a registry"; else bad "a side-loaded image did not run"; fi

  # 6. Image-pull failure, reproducibly: preflight treats it as its own signal (ADR-05-07).
  kubectl -n "$CHECK_NAMESPACE" run pull-failure --image registry.invalid/test-env/absent:1 --restart Never >/dev/null
  local reason=""
  for _ in $(seq 1 45); do
    reason="$(kubectl -n "$CHECK_NAMESPACE" get pod pull-failure -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)"
    [[ "$reason" == ErrImagePull || "$reason" == ImagePullBackOff ]] && break
    sleep 2
  done
  if [[ "$reason" == ErrImagePull || "$reason" == ImagePullBackOff ]]; then ok "an unpullable image fails as $reason"; else bad "an unpullable image did not fail to pull (state: ${reason:-none})"; fi

  # 7. NetworkPolicy: can connectivity be made to fail on purpose? Measured, then compared.
  kubectl apply -f - >/dev/null <<YAML
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: deny-ingress, namespace: $CHECK_NAMESPACE}
spec: {podSelector: {}, policyTypes: [Ingress]}
YAML
  local blocked=no
  for _ in 1 2 3 4 5 6; do
    [[ -z "$(fetch_from probe-0 "http://${ip[1]}:8080")" ]] && { blocked=yes; break; }
    sleep 2
  done
  local expected; expected="$(enforces_network_policy "$profile")"
  if [[ "$blocked" == "$expected" ]]; then
    if [[ "$blocked" == yes ]]; then ok "NetworkPolicy is enforced: a deny policy blocks pod traffic"; else ok "NetworkPolicy is not enforced by this CNI, as expected"; fi
  else
    bad "NetworkPolicy enforcement is '$blocked', but this profile is recorded as '$expected'"
  fi
  kubectl -n "$CHECK_NAMESPACE" delete networkpolicy deny-ingress >/dev/null
  local restored=no
  for _ in $(seq 1 15); do [[ "$(fetch_from probe-0 "http://${ip[1]}:8080")" == probe-1 ]] && { restored=yes; break; }; sleep 2; done
  if [[ "$restored" == yes ]]; then ok "traffic recovers once the policy is removed"; else bad "traffic did not recover after the policy was removed"; fi

  finish_check "$profile"
}

finish_check() {
  kubectl delete namespace "$CHECK_NAMESPACE" --ignore-not-found --wait=false >/dev/null
  local version; version="$(kubectl version -o json 2>/dev/null | awk -F'"' '/"gitVersion"/ {v=$4} END {print v}')"
  echo
  echo "  profile: $1   Kubernetes: $version"
  echo "  This environment validates orchestration correctness, not measurement realism:"
  echo "  every node shares one host's network stack (ADR-05-11). Throughput, loss and"
  echo "  jitter measured here say nothing about real hardware."
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo "### Test environment \`$1\` — $([[ $failures == 0 ]] && echo passed || echo "$failures check(s) failed")"
      echo
      echo "> **Not representative.** This environment validates orchestration correctness, not measurement realism."
      echo "> Every node shares one host's network stack, so throughput, loss and jitter measured here say nothing about real hardware (ADR-05-11)."
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  if [[ "$failures" -gt 0 ]]; then die "$failures check(s) failed for $1"; fi
  say "every check passed for $1"
}

# -- down ----------------------------------------------------------------------------------

cmd_down() {
  local profile tool leftovers
  profile="$(active_profile)"
  tool="$(tool_of "$profile")"
  require_tools docker "$tool"
  # The delete itself is allowed to fail: after a failed `up` there may be a half-made
  # cluster, or none. What `down` guarantees is the check below, that nothing labelled with
  # this cluster remains, and that is what decides whether it succeeds.
  case "$tool" in
    kind) kind delete cluster --name "$cluster" --kubeconfig "$KUBECONFIG" >/dev/null 2>&1 || true ;;
    k3d) k3d cluster delete "$cluster" >/dev/null 2>&1 || true ;;
  esac

  # "Reproducible teardown" means provably nothing left: no container, volume or network
  # carrying this cluster's label. kind's shared `kind` network is kind's own, reused across
  # clusters, and deliberately left in place.
  local label
  case "$tool" in kind) label="io.x-k8s.kind.cluster=$cluster" ;; k3d) label="k3d.cluster=$cluster" ;; esac
  leftovers="$(docker ps -aq --filter "label=$label"; docker volume ls -q --filter "label=$label"; docker network ls -q --filter "label=$label")"
  rm -f "$state/profile" "$KUBECONFIG" "$state/calico.yaml" "$state/calico.yaml.orig" "$state/kube-flannel.yml"
  [[ -z "$leftovers" ]] || die "teardown left Docker objects labelled $label behind: $(echo "$leftovers" | tr '\n' ' ')"
  say "$profile is down; nothing labelled $label remains"
}

cmd_profiles() {
  local profile
  for profile in "${PROFILES[@]}"; do
    printf '%-14s %-5s %-8s NetworkPolicy enforced: %s\n' "$profile" "$(tool_of "$profile")" "$(cni_of "$profile")" "$(enforces_network_policy "$profile")"
  done
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  check) cmd_check ;;
  nodes) cmd_nodes ;;
  load) shift; cmd_load "$@" ;;
  down) cmd_down ;;
  profiles) cmd_profiles ;;
  *) sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
