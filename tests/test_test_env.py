"""The test environment's configs, scripts, docs and CI agree with each other (M1-18).

None of this provisions a cluster — CI's test-environment workflow does that, for every
profile. These tests guard the promises that are easy to break with a one-line edit and
invisible until someone provisions: that every profile has the same shape, that versions
live in one place and are pinned by digest, that the list of profiles is the same in the
script, the workflow and the README, and that the scripts still run on macOS's bash 3.2.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
import yaml

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
TEST_ENV = REPOSITORY / "test-env"
SCRIPT = TEST_ENV / "test-env.sh"
WORKFLOW = REPOSITORY / ".github" / "workflows" / "test-env.yml"

WORKERS = 3
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _versions() -> dict[str, str]:
    pairs = (
        line.split("=", 1)
        for line in (TEST_ENV / "versions.env").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    return {key.strip(): value.strip() for key, value in pairs}


def _yaml(path: pathlib.Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


def _script_profiles() -> list[str]:
    found = re.search(r"^PROFILES=\(([^)]*)\)", SCRIPT.read_text(), re.M)
    assert found, "test-env.sh no longer declares PROFILES"
    return found.group(1).split()


def _script_enforcement(profile: str) -> str:
    """What test-env.sh records for a profile's NetworkPolicy enforcement."""
    body = re.search(r"enforces_network_policy\(\) \{\n(.*?)\n\}", SCRIPT.read_text(), re.S)
    assert body, "test-env.sh no longer defines enforces_network_policy"
    for patterns, answer in re.findall(r"^[ \t]+([^)\n]+)\) echo (\w+) ;;", body.group(1), re.M):
        if any(re.fullmatch(p.strip().replace("*", ".*"), profile) for p in patterns.split("|")):
            return str(answer)
    raise AssertionError(f"enforces_network_policy has no answer for {profile}")


# -- every profile has the same shape ---------------------------------------------------


@pytest.mark.parametrize("config", ["cluster.yaml", "cluster-no-cni.yaml"])
def test_kind_clusters_are_one_control_plane_and_three_workers(config: str) -> None:
    roles = [node["role"] for node in _yaml(TEST_ENV / "kind" / config)["nodes"]]

    assert roles.count("control-plane") == 1
    assert roles.count("worker") == WORKERS


@pytest.mark.parametrize("config", ["cluster.yaml", "cluster-no-cni.yaml"])
def test_k3d_clusters_are_one_server_and_three_agents(config: str) -> None:
    cluster = _yaml(TEST_ENV / "k3d" / config)

    assert cluster["servers"] == 1
    assert cluster["agents"] == WORKERS


@pytest.mark.parametrize("config", ["cluster.yaml", "cluster-no-cni.yaml"])
def test_the_k3d_server_is_tainted_like_kinds_control_plane(config: str) -> None:
    # k3s makes its server schedulable by default. Without the taint a k3d cluster would
    # have four places a test pod could land and a kind cluster three.
    args = [
        entry["arg"] for entry in _yaml(TEST_ENV / "k3d" / config)["options"]["k3s"]["extraArgs"]
    ]

    assert "--node-taint=node-role.kubernetes.io/control-plane:NoSchedule" in args


def test_the_no_cni_variants_really_have_no_cni() -> None:
    kind = _yaml(TEST_ENV / "kind" / "cluster-no-cni.yaml")
    k3d_args = [
        entry["arg"]
        for entry in _yaml(TEST_ENV / "k3d" / "cluster-no-cni.yaml")["options"]["k3s"]["extraArgs"]
    ]

    assert kind["networking"]["disableDefaultCNI"] is True
    assert "--flannel-backend=none" in k3d_args
    # Otherwise k3s's embedded policy controller would enforce NetworkPolicy alongside the
    # installed CNI, and the measured enforcement would not be the CNI's.
    assert "--disable-network-policy" in k3d_args


def test_the_pod_network_a_cni_is_given_is_the_one_the_tool_uses() -> None:
    script = SCRIPT.read_text()

    for config in ("cluster.yaml", "cluster-no-cni.yaml"):
        assert _yaml(TEST_ENV / "kind" / config)["networking"]["podSubnet"] == "10.244.0.0/16"
    assert "kind) echo 10.244.0.0/16" in script
    assert "k3d) echo 10.42.0.0/16" in script  # k3s's default --cluster-cidr


# -- versions live in one place, pinned ---------------------------------------------------


def test_images_are_pinned_by_digest() -> None:
    versions = _versions()

    for key in ("KIND_NODE_IMAGE", "K3S_IMAGE", "CHECK_IMAGE"):
        assert DIGEST.search(versions[key]), f"{key} is not pinned by digest"


def test_manifests_are_pinned_by_checksum() -> None:
    versions = _versions()

    for key in ("CALICO_MANIFEST_SHA256", "FLANNEL_MANIFEST_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", versions[key]), f"{key} is not a sha256"


def test_both_tools_run_the_same_kubernetes() -> None:
    versions = _versions()
    kind = re.search(r":(v\d+\.\d+\.\d+)@", versions["KIND_NODE_IMAGE"])
    k3s = re.search(r":(v\d+\.\d+\.\d+)-k3s", versions["K3S_IMAGE"])

    assert kind and k3s
    assert kind.group(1) == k3s.group(1)


def _keys(value: object) -> set[str]:
    """Every mapping key anywhere in a parsed YAML document."""
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _keys(child)}
    return set()


@pytest.mark.parametrize(
    "path",
    [*sorted((TEST_ENV / "kind").glob("*.yaml")), *sorted((TEST_ENV / "k3d").glob("*.yaml"))],
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_no_config_names_an_image_of_its_own(path: pathlib.Path) -> None:
    # The image is passed from versions.env with --image. A second copy here would be a
    # second place to update, and the one that gets forgotten.
    assert "image" not in _keys(_yaml(path))


# -- one list of profiles -------------------------------------------------------------------


def test_the_workflow_runs_every_profile_the_script_knows() -> None:
    workflow = _yaml(WORKFLOW)
    matrix = workflow["jobs"]["environment"]["strategy"]["matrix"]["profile"]

    assert matrix == _script_profiles()


def test_the_readme_lists_every_profile_with_its_measured_enforcement() -> None:
    readme = (TEST_ENV / "README.md").read_text()
    rows = dict(
        re.findall(r"^\| `([a-z0-9-]+)` \|[^|]+\|[^|]+\| (\*\*no\*\*|no|yes)[^|]*\|", readme, re.M)
    )

    assert sorted(rows) == sorted(_script_profiles())
    for profile, documented in rows.items():
        assert documented.strip("*") == _script_enforcement(profile), profile


# -- scripts --------------------------------------------------------------------------------


@pytest.mark.parametrize("script", ["test-env.sh", "install-tools.sh"])
def test_the_scripts_run_on_macos_bash_3_2(script: str) -> None:
    # macOS still ships bash 3.2 as /bin/bash. Associative arrays, mapfile and case
    # conversion arrived in bash 4, and a developer without Homebrew's bash would get a
    # syntax error rather than an environment.
    text = (TEST_ENV / script).read_text()

    for construct in ("declare -A", "mapfile", "readarray", ",,}", "^^}"):
        assert construct not in text, f"{script} uses {construct!r}, which needs bash 4"


@pytest.mark.parametrize("script", ["test-env.sh", "install-tools.sh"])
def test_arrays_that_can_be_empty_are_expanded_safely(script: str) -> None:
    # Under `set -u`, bash 3.2 treats "${a[@]}" of an empty array as an unbound variable.
    # That broke kind-cilium on macOS while it worked on Linux's bash 5, where CI runs.
    # `${a[@]+"${a[@]}"}` is the form that works on both.
    code = "\n".join(
        line
        for line in (TEST_ENV / script).read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    text = code
    may_be_empty = set(re.findall(r"\b([a-z_]+)=\(\)", text))

    for name in may_be_empty:
        unguarded = re.findall(r'(?<!\+)"\$\{' + name + r"\[@\]\}\"", text)
        assert not unguarded, f"{script} expands the possibly empty array {name!r} unguarded"


def test_the_scripts_are_executable() -> None:
    for script in ("test-env.sh", "install-tools.sh"):
        assert (TEST_ENV / script).stat().st_mode & 0o111, f"{script} is not executable"


def test_the_realism_boundary_is_stated_where_a_ci_reader_looks() -> None:
    # ADR-05-11: a green run must not be read as evidence of network performance.
    script = SCRIPT.read_text()

    assert "GITHUB_STEP_SUMMARY" in script
    assert "not measurement realism" in script


def test_files_bash_reads_keep_unix_line_endings_on_every_checkout() -> None:
    # Windows is supported through WSL2, where a checkout with core.autocrlf=true would
    # otherwise hand bash CRLF scripts and a versions.env whose every value ends in \r.
    rules = (REPOSITORY / ".gitattributes").read_text().split("\n")

    for pattern in ("*.sh", "*.env"):
        assert any(r.split()[:1] == [pattern] and "eol=lf" in r for r in rules), pattern


def test_the_only_sudo_is_behind_load_modules() -> None:
    # Nothing changes the host kernel unless the user asked: either they run the command
    # themselves, or they pass --load-modules and are told first. So the script may run
    # sudo in exactly one place, and only after that flag was checked.
    lines = SCRIPT.read_text().splitlines()
    sudo = [i for i, line in enumerate(lines) if re.match(r"^\s*sudo ", line)]

    assert len(sudo) == 1, f"expected exactly one sudo, found lines {[i + 1 for i in sudo]}"
    before = "\n".join(lines[: sudo[0]])
    function = before[before.rindex("require_br_netfilter() {") :]
    assert 'if [[ "$1" != yes ]]; then' in function
    assert "die " in function.split('if [[ "$1" != yes ]]; then', 1)[1]
    assert (TEST_ENV / "install-tools.sh").read_text().count("sudo") == 0


def test_up_records_the_profile_before_it_creates_anything() -> None:
    # `down` tears down whichever profile is recorded. Recorded only after creation, a
    # failure inside the create command left a cluster that `down` refused to touch.
    body = re.search(r"^cmd_up\(\) \{\n(.*?)\n\}", SCRIPT.read_text(), re.S | re.M)
    assert body, "test-env.sh no longer defines cmd_up"
    up = body.group(1)
    recorded = up.index('echo "$profile" > "$state/profile"')

    for create in ("kind create cluster", "k3d cluster create"):
        command = re.search(r"^[ \t]+" + create, up, re.M)  # the command, not a comment
        assert command, f"cmd_up no longer runs {create}"
        assert recorded < command.start(), f"the profile is recorded after {create}"
    assert up.count('> "$state/profile"') == 1
