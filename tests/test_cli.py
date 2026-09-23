"""The CLI's image resolution: the override has to actually reach the resolver."""

from __future__ import annotations

import pytest

from test_orchestrator import images
from test_orchestrator.cli.main import main
from test_orchestrator.images import IMAGE_OVERRIDE_ENV_VAR, IMAGE_REPOSITORY

DIGEST = "sha256:" + "ab" * 32


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMAGE_OVERRIDE_ENV_VAR, raising=False)


def test_version_still_works(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("test-orchestrator ")


def test_image_prints_the_released_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", DIGEST)

    assert main(["image"]) == 0
    out = capsys.readouterr().out
    assert f"{IMAGE_REPOSITORY}@{DIGEST}" in out
    assert "source: release-default" in out


def test_image_honours_the_run_override(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["image", "--orchestrator-image", "registry.internal/orchestrator:1.2.3"]) == 0
    out = capsys.readouterr().out
    assert "registry.internal/orchestrator:1.2.3" in out
    assert "source: run-override" in out
    assert "not pinned by digest" in out


def test_image_honours_the_installation_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(IMAGE_OVERRIDE_ENV_VAR, f"registry.internal/orchestrator@{DIGEST}")

    assert main(["image"]) == 0
    assert "source: installation-override" in capsys.readouterr().out


def test_image_on_a_development_build_fails_with_an_explanation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["image"]) == 1
    assert "development build" in capsys.readouterr().err


def test_unknown_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["submit"]) == 2
    assert "unknown command" in capsys.readouterr().err
