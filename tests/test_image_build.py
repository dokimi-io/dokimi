"""The Dockerfile and the release wiring must keep the promises ADR-05-08 makes.

These are text assertions, which is unusual, and deliberate. The base image choice, the
lockfile-only install and the non-root user are *decisions* with reasons behind them
([ADR-05-08], [ADR-05-04]); CI catches a broken build, but only something like this
catches a build that still works while having quietly changed its mind.

The last test is the one that matters most: the release workflow rewrites a line in a
generated file, and a reformat of that file would break the release without breaking
anything a normal test run would notice.
"""

from __future__ import annotations

import pathlib
import re
import textwrap

import pytest
import yaml

from dokimi import _image_digest, images

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = REPOSITORY_ROOT / "images" / "orchestrator" / "Dockerfile"
IPERF2_DOCKERFILE = REPOSITORY_ROOT / "images" / "iperf2" / "Dockerfile"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
GENERATED_DIGEST = REPOSITORY_ROOT / "src" / "dokimi" / "_image_digest.py"


def _instructions() -> str:
    """The Dockerfile with its comments stripped — the prose mentions Alpine on purpose."""
    lines = DOCKERFILE.read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_base_image_is_debian_slim_not_alpine() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "FROM python:${PYTHON_VERSION}-slim AS runtime" in dockerfile
    assert "alpine" not in _instructions().lower()


def test_the_image_python_matches_the_declared_floor() -> None:
    # Derived rather than hard-coded: these drifted apart once already, when the floor
    # moved to 3.14 and the Dockerfile kept building on 3.12 (ADR-01-09).
    pyproject = PYPROJECT.read_text()
    floor = re.search(r'requires-python = ">=(\d+\.\d+)"', pyproject)
    assert floor, "pyproject.toml no longer declares requires-python"

    assert f"ARG PYTHON_VERSION={floor.group(1)}" in DOCKERFILE.read_text(), (
        f"the image builds on a different Python than the declared floor "
        f"{floor.group(1)}, so the image and the CLI distribution are not the same "
        "environment"
    )


def test_the_image_uv_matches_the_enforced_floor() -> None:
    # `[tool.uv] required-version` makes uv refuse to run below the floor, so a stale
    # ARG here is a broken build rather than a silent downgrade. Catch it in a test
    # instead of in a release.
    pyproject = PYPROJECT.read_text()
    floor = re.search(r'required-version = ">=(\S+?)"', pyproject)
    assert floor, "pyproject.toml no longer enforces a uv floor"

    assert f"ARG UV_VERSION={floor.group(1)}" in DOCKERFILE.read_text()


def test_dependencies_come_from_the_lockfile_alone() -> None:
    # `--locked` fails rather than re-resolving, which is what makes the image and the
    # CLI distribution provably the same dependency set.
    instructions = _instructions()
    assert "uv sync --locked" in instructions
    assert "pip install" not in instructions


def test_the_image_runs_as_a_non_root_user() -> None:
    user_lines = [line for line in DOCKERFILE.read_text().splitlines() if line.startswith("USER ")]

    assert user_lines, "the Dockerfile never drops from root"
    assert not user_lines[-1].startswith("USER root")
    assert user_lines[-1] == "USER 65532:65532"


@pytest.mark.parametrize("part", ["resources", "libraries"])
def test_the_image_carries_what_the_rendered_suite_imports(part: str) -> None:
    # The suite imports resources/Dokimi.resource, which imports the library adapter.
    # Without both in the image, every pair's Robot process fails before its first keyword.
    ignore = (REPOSITORY_ROOT / ".dockerignore").read_text().splitlines()

    assert f"!robot/{part}" in ignore
    assert f"COPY --chown=root:root robot/{part} /opt/dokimi/robot/{part}" in _instructions()
    assert "DOKIMI_ROBOT_DIR=/opt/dokimi/robot" in DOCKERFILE.read_text()


def test_the_checked_in_digest_is_unset() -> None:
    # A source checkout is not a release. If this ever lands set, some build is shipping
    # a digest it did not produce.
    assert "ORCHESTRATOR_IMAGE_DIGEST: str | None = None" in GENERATED_DIGEST.read_text()


def _built_images() -> list[dict[str, str]]:
    """The release's `images` matrix: every image dokimi builds and publishes itself."""
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    entries: list[dict[str, str]] = workflow["jobs"]["images"]["strategy"]["matrix"]["include"]
    return entries


def _digest_constants() -> set[str]:
    return set(re.findall(r"^([A-Z0-9_]+_IMAGE_DIGEST): str", GENERATED_DIGEST.read_text(), re.M))


def _constant(name: str) -> str:
    return f"{name.upper().replace('-', '_')}_IMAGE_DIGEST"


def test_every_built_image_has_a_dockerfile_and_a_digest_constant() -> None:
    # A digest constant no image fills stays None in a release, and an image without a
    # constant is published but never resolved: either way every released CLI behaves
    # as a development checkout for it. The release refuses both; this catches them first.
    built = _built_images()

    assert {entry["name"] for entry in built} == {"orchestrator", "iperf2"}
    for entry in built:
        assert (REPOSITORY_ROOT / entry["file"]).is_file(), entry["file"]
        assert entry["image"] == f"ghcr.io/dokimi-io/{entry['name']}", entry
    assert {_constant(entry["name"]) for entry in built} == _digest_constants()


def _release_script(step: str) -> str:
    """The inline Python of one release step, as the workflow holds it."""
    workflow = RELEASE_WORKFLOW.read_text()
    after = workflow[workflow.index(step) :]
    script = re.search(r"python - <<'PY'\n(.*?)\n\s*PY\n", after, re.S)
    assert script, f"the release step {step!r} no longer runs an inline script"
    return textwrap.dedent(script.group(1))


def _digests(directory: pathlib.Path) -> dict[str, str]:
    """What each matrix leg hands on: a file per image holding its digest-pinned reference."""
    (directory / "digests").mkdir()
    references = {}
    for number, entry in enumerate(_built_images()):
        reference = f"{entry['image']}@sha256:{f'{number:x}' * 64}"
        (directory / "digests" / entry["name"]).write_text(reference + "\n")
        references[entry["name"]] = reference
    return references


def test_the_release_bakes_every_digest_into_the_generated_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = tmp_path / "src" / "dokimi" / "_image_digest.py"
    generated.parent.mkdir(parents=True)
    generated.write_text(GENERATED_DIGEST.read_text())
    references = _digests(tmp_path)
    monkeypatch.chdir(tmp_path)

    exec(compile(_release_script("Bake the image digests"), "release.yml", "exec"), {})

    baked = generated.read_text()
    for name, reference in references.items():
        digest = reference.partition("@")[2]
        assert f'{_constant(name)}: str | None = "{digest}"' in baked


def test_the_release_refuses_an_image_without_a_digest_constant(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated = tmp_path / "src" / "dokimi" / "_image_digest.py"
    generated.parent.mkdir(parents=True)
    generated.write_text(GENERATED_DIGEST.read_text())
    _digests(tmp_path)
    (tmp_path / "digests" / "irtt").write_text(f"ghcr.io/dokimi-io/irtt@sha256:{'e' * 64}\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit, match="do not match one to one"):
        exec(compile(_release_script("Bake the image digests"), "release.yml", "exec"), {})
    assert generated.read_text() == GENERATED_DIGEST.read_text()


def test_the_release_verification_runs_against_this_code(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It runs only in a release, after the images are pushed. An import it can no longer
    # make shows up there first, as a release with published images and no CLI. It did
    # once: dokimi.images.resolve_image went away with ADR-04-08.
    references = _digests(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        images, "ORCHESTRATOR_IMAGE_DIGEST", references["orchestrator"].partition("@")[2]
    )
    monkeypatch.setattr(
        _image_digest, "IPERF2_IMAGE_DIGEST", references["iperf2"].partition("@")[2]
    )

    script = _release_script("Verify the baked-in defaults")
    exec(compile(script, "release.yml", "exec"), {"__name__": "__main__"})


def test_the_checked_in_workload_digests_are_unset() -> None:
    assert "IPERF2_IMAGE_DIGEST: str | None = None" in GENERATED_DIGEST.read_text()


def _iperf2_instructions() -> str:
    lines = IPERF2_DOCKERFILE.read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_the_iperf2_image_is_alpine_pinned_by_digest() -> None:
    # ADR-05-04: slim Alpine for test workloads, and a tag is a pointer that can move.
    dockerfile = IPERF2_DOCKERFILE.read_text()

    assert re.search(r"^ARG ALPINE_DIGEST=sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert "FROM alpine:${ALPINE_VERSION}@${ALPINE_DIGEST}" in dockerfile


def test_the_iperf2_package_is_pinned_to_an_exact_version() -> None:
    # The exporter parses iperf2's output; an unpinned apk add is a different parser input.
    dockerfile = IPERF2_DOCKERFILE.read_text()

    assert re.search(r"^ARG IPERF_VERSION=\d+\.\d+\.\d+-r\d+$", dockerfile, re.M)
    assert 'apk add --no-cache "iperf=${IPERF_VERSION}"' in _iperf2_instructions()


def test_the_iperf2_image_runs_as_a_non_root_user() -> None:
    user_lines = [
        line for line in IPERF2_DOCKERFILE.read_text().splitlines() if line.startswith("USER ")
    ]

    assert user_lines and user_lines[-1] == "USER 65532:65532"


IPERF2_SCRIPTS = ("dokimi-iperf2-client", "dokimi-iperf2-server")


@pytest.mark.parametrize("command", IPERF2_SCRIPTS)
def test_each_iperf2_script_is_in_the_build_context(command: str) -> None:
    # .dockerignore excludes everything by default; a script it drops fails the COPY.
    script = f"images/iperf2/{command}.sh"
    ignore = (REPOSITORY_ROOT / ".dockerignore").read_text().splitlines()

    assert f"!{script}" in ignore
    assert f"COPY --chmod=0755 {script} /usr/local/bin/{command}" in _iperf2_instructions()


@pytest.mark.parametrize("command", IPERF2_SCRIPTS)
def test_each_iperf2_script_has_unix_line_endings(command: str) -> None:
    # A CRLF breaks a shell script outright; .gitattributes keeps *.sh as LF.
    script = REPOSITORY_ROOT / "images" / "iperf2" / f"{command}.sh"

    assert b"\r\n" not in script.read_bytes()


@pytest.mark.parametrize("dockerfile", [DOCKERFILE, IPERF2_DOCKERFILE])
def test_each_image_is_labelled_with_the_project_license(dockerfile: pathlib.Path) -> None:
    assert (REPOSITORY_ROOT / "LICENSE").read_text().startswith("MIT License")
    assert 'org.opencontainers.image.licenses="MIT"' in dockerfile.read_text()
