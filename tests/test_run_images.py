"""The images a run deploys: chosen at submission, carried into the Job (ADR-04-08)."""

from __future__ import annotations

import pytest

from dokimi import _image_digest
from dokimi.images import ImageSource
from dokimi.model import ConfigError
from dokimi.run_images import (
    COMPONENTS,
    images_from_job_environment,
    job_environment,
    parse_image_options,
    resolve_run_images,
)
from dokimi.testtypes.interface import WorkloadImage

DIGEST = "sha256:" + "cd" * 32
RELEASED = f"ghcr.io/dokimi-io/iperf2@{DIGEST}"


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_image_digest, "IPERF2_IMAGE_DIGEST", DIGEST)


@pytest.fixture
def source_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_image_digest, "IPERF2_IMAGE_DIGEST", None)


# -- choosing ---------------------------------------------------------------------------


def test_every_component_is_named_and_has_an_environment_variable() -> None:
    assert sorted(COMPONENTS) == ["iperf2"]
    assert COMPONENTS["iperf2"].env_var == "DOKIMI_IPERF2_IMAGE"
    assert COMPONENTS["iperf2"].source_env_var == "DOKIMI_IPERF2_IMAGE_SOURCE"


def test_a_released_build_runs_its_own_digest(released: None) -> None:
    assert resolve_run_images(env={}) == {
        "iperf2": WorkloadImage(RELEASED, ImageSource.RELEASE_DEFAULT)
    }


def test_an_installation_override_beats_the_release_default(released: None) -> None:
    images = resolve_run_images(env={"DOKIMI_IPERF2_IMAGE": "mirror.local/iperf2:2.2.1"})

    assert images["iperf2"] == WorkloadImage(
        "mirror.local/iperf2:2.2.1", ImageSource.INSTALLATION_OVERRIDE
    )


def test_a_run_override_beats_both(released: None) -> None:
    images = resolve_run_images(
        {"iperf2": "iperf2:dev"}, env={"DOKIMI_IPERF2_IMAGE": "mirror.local/iperf2:2.2.1"}
    )

    assert images["iperf2"] == WorkloadImage("iperf2:dev", ImageSource.RUN_OVERRIDE)


def test_a_blank_installation_override_is_no_override(released: None) -> None:
    images = resolve_run_images(env={"DOKIMI_IPERF2_IMAGE": "   "})

    assert images["iperf2"].source is ImageSource.RELEASE_DEFAULT


def test_a_source_build_without_an_override_is_refused_and_says_how(source_build: None) -> None:
    # Before anything is created, rather than a Job that fails once it is running.
    with pytest.raises(ConfigError, match=r"--image iperf2=<reference> or DOKIMI_IPERF2_IMAGE"):
        resolve_run_images(env={})


def test_a_source_build_runs_with_an_override(source_build: None) -> None:
    images = resolve_run_images(env={"DOKIMI_IPERF2_IMAGE": "iperf2:dev"})

    assert images["iperf2"] == WorkloadImage("iperf2:dev", ImageSource.INSTALLATION_OVERRIDE)


# -- --image ----------------------------------------------------------------------------


def test_image_options_become_a_mapping() -> None:
    assert parse_image_options(["iperf2=mirror.local/iperf2@" + DIGEST]) == {
        "iperf2": "mirror.local/iperf2@" + DIGEST
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("iperf2", "is not <component>=<reference>"),
        ("=iperf2:dev", "is not <component>=<reference>"),
        ("iperf3=iperf3:dev", "no component 'iperf3'"),
        ("iperf2=", "is not an image reference"),
        ("iperf2=has space", "is not an image reference"),
        ("iperf2=-flag", "is not an image reference"),
    ],
)
def test_a_malformed_image_option_is_a_config_error(value: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_image_options([value])


def test_a_component_given_twice_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="more than once"):
        parse_image_options(["iperf2=a:1", "iperf2=b:2"])


def test_an_unknown_component_is_refused_when_resolving_too(released: None) -> None:
    with pytest.raises(ConfigError, match="no component 'trex'"):
        resolve_run_images({"trex": "trex:dev"}, env={})


# -- into the Job, and back -------------------------------------------------------------


def test_the_job_environment_carries_reference_and_source() -> None:
    images = {"iperf2": WorkloadImage("iperf2:dev", ImageSource.RUN_OVERRIDE)}

    assert job_environment(images) == [
        {"name": "DOKIMI_IPERF2_IMAGE", "value": "iperf2:dev"},
        {"name": "DOKIMI_IPERF2_IMAGE_SOURCE", "value": "run-override"},
    ]


def test_the_job_reads_back_exactly_what_submit_wrote() -> None:
    images = {
        "iperf2": WorkloadImage("mirror.local/iperf2:2.2.1", ImageSource.INSTALLATION_OVERRIDE)
    }
    environment = {entry["name"]: entry["value"] for entry in job_environment(images)}

    assert images_from_job_environment(environment) == images


def test_the_job_never_falls_back_to_a_default(released: None) -> None:
    # A Job without its images was not made by this CLI. Guessing a default inside the
    # cluster would run something the submitter never chose or saw.
    with pytest.raises(ConfigError, match="submitted without an image for iperf2"):
        images_from_job_environment({})


def test_the_job_refuses_a_source_it_does_not_know() -> None:
    environment = {"DOKIMI_IPERF2_IMAGE": "iperf2:dev", "DOKIMI_IPERF2_IMAGE_SOURCE": "guess"}

    with pytest.raises(ConfigError, match="not an image source"):
        images_from_job_environment(environment)
