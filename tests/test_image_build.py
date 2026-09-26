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


def test_the_release_workflow_still_matches_the_line_it_rewrites() -> None:
    workflow = RELEASE_WORKFLOW.read_text()
    found = re.search(r'r"(\^ORCHESTRATOR_IMAGE_DIGEST[^"]*)"', workflow)
    assert found, "the release workflow no longer rewrites the generated digest file"

    pattern = re.compile(found.group(1), re.MULTILINE)
    assert pattern.search(GENERATED_DIGEST.read_text()), (
        f"the release workflow looks for {found.group(1)!r}, which no longer matches "
        f"anything in {GENERATED_DIGEST.name}. A release would build a distribution with "
        "no image digest baked in."
    )
