#!/usr/bin/env bash
# Install the pinned kind, k3d, kubectl and cilium CLIs into a directory, verified.
#
#   test-env/install-tools.sh [DIR]      default: ./.test-env/bin
#
# Each binary is checked against the checksum published with that same release. That
# catches a corrupted or truncated download; it does not defend against a compromised
# release, which would need hashes pinned here rather than fetched alongside the binary.
# Versions come from versions.env and nowhere else.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=test-env/versions.env
source "$here/versions.env"

dest="${1:-.test-env/bin}"
mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

verify() { # verify FILE EXPECTED_SHA256
  local actual; actual="$(sha256 "$1")"
  if [[ "$actual" != "$2" ]]; then
    echo "checksum mismatch for $(basename "$1"): expected $2, got $actual" >&2
    exit 1
  fi
}

fetch() { curl -fsSL --retry 3 -o "$2" "$1"; }

echo "installing into $dest ($os/$arch)"

# kind: one .sha256sum per binary
fetch "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-$os-$arch" "$work/kind"
fetch "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-$os-$arch.sha256sum" "$work/kind.sha256"
verify "$work/kind" "$(cut -d' ' -f1 "$work/kind.sha256")"
install -m 0755 "$work/kind" "$dest/kind"

# k3d: one checksums.txt for the whole release
fetch "https://github.com/k3d-io/k3d/releases/download/$K3D_VERSION/k3d-$os-$arch" "$work/k3d"
fetch "https://github.com/k3d-io/k3d/releases/download/$K3D_VERSION/checksums.txt" "$work/k3d.checksums"
verify "$work/k3d" "$(awk -v f="k3d-$os-$arch" '$2 == f || $2 == "_dist/"f {print $1}' "$work/k3d.checksums")"
install -m 0755 "$work/k3d" "$dest/k3d"

# kubectl: dl.k8s.io publishes a bare hash beside each binary
fetch "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$os/$arch/kubectl" "$work/kubectl"
fetch "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$os/$arch/kubectl.sha256" "$work/kubectl.sha256"
verify "$work/kubectl" "$(tr -d '[:space:]' < "$work/kubectl.sha256")"
install -m 0755 "$work/kubectl" "$dest/kubectl"

# cilium CLI: a tarball with a .sha256sum
fetch "https://github.com/cilium/cilium-cli/releases/download/$CILIUM_CLI_VERSION/cilium-$os-$arch.tar.gz" "$work/cilium.tgz"
fetch "https://github.com/cilium/cilium-cli/releases/download/$CILIUM_CLI_VERSION/cilium-$os-$arch.tar.gz.sha256sum" "$work/cilium.sha256"
verify "$work/cilium.tgz" "$(cut -d' ' -f1 "$work/cilium.sha256")"
tar -xzf "$work/cilium.tgz" -C "$work" cilium
install -m 0755 "$work/cilium" "$dest/cilium"

for tool in kind k3d kubectl cilium; do
  printf '  %-8s %s\n' "$tool" "$("$dest/$tool" version 2>/dev/null | head -1 || true)"
done
echo "add it to PATH: export PATH=\"$dest:\$PATH\""
