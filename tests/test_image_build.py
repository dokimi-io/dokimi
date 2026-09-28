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


def test_the_checked_in_digest_is_unset() -> None:
    # A source checkout is not a release. If this ever lands set, some build is shipping
    # a digest it did not produce.
    assert "ORCHESTRATOR_IMAGE_DIGEST: str | None = None" in GENERATED_DIGEST.read_text()


def test_the_release_workflow_still_matches_the_lines_it_rewrites() -> None:
    workflow = RELEASE_WORKFLOW.read_text()
    patterns = re.findall(r'r"(\^[A-Z0-9_]+_IMAGE_DIGEST[^"]*)"', workflow)
    assert patterns, "the release workflow no longer rewrites the generated digest file"

    generated = GENERATED_DIGEST.read_text()
    for found in patterns:
        assert re.search(found, generated, re.MULTILINE), (
            f"the release workflow looks for {found!r}, which no longer matches anything "
            f"in {GENERATED_DIGEST.name}. A release would build a distribution with no "
            "image digest baked in."
        )


def test_the_release_workflow_bakes_every_generated_digest() -> None:
    # A digest constant nobody rewrites stays None in a release, and that image's
    # resolution fails on every released CLI, as if it were a development checkout.
    constants = set(
        re.findall(r"^([A-Z0-9_]+_IMAGE_DIGEST): str", GENERATED_DIGEST.read_text(), re.M)
    )
    rewritten = set(re.findall(r'r"\^([A-Z0-9_]+_IMAGE_DIGEST):', RELEASE_WORKFLOW.read_text()))

    assert constants == {"ORCHESTRATOR_IMAGE_DIGEST", "IPERF2_IMAGE_DIGEST"}
    assert rewritten == constants


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


def test_the_iperf2_client_script_is_in_the_build_context() -> None:
    # .dockerignore excludes everything by default; a script it drops fails the COPY.
    script = "images/iperf2/dokimi-iperf2-client.sh"
    ignore = (REPOSITORY_ROOT / ".dockerignore").read_text().splitlines()

    assert f"!{script}" in ignore
    assert f"COPY --chmod=0755 {script} /usr/local/bin/dokimi-iperf2-client" in (
        _iperf2_instructions()
    )


def test_the_iperf2_client_script_has_unix_line_endings() -> None:
    # A CRLF breaks a shell script outright; .gitattributes keeps *.sh as LF.
    script = REPOSITORY_ROOT / "images" / "iperf2" / "dokimi-iperf2-client.sh"

    assert b"\r\n" not in script.read_bytes()
