"""The images a run deploys: chosen at submission, carried into the Job."""

from __future__ import annotations

import pytest

from dokimi import _image_digest, images
from dokimi.images import ImageSource
from dokimi.metrics.managed import NODE_EXPORTER_IMAGE, PROMETHEUS_IMAGE
from dokimi.model import ConfigError
from dokimi.run_images import (
    COMPONENTS,
    images_from_job_environment,
    job_environment,
    parse_image_options,
    resolve_component,
    resolve_run_images,
)
from dokimi.testtypes.interface import WorkloadImage

DIGEST = "sha256:" + "cd" * 32
RELEASED = f"ghcr.io/dokimi-io/iperf2@{DIGEST}"
RELEASED_ORCHESTRATOR = f"ghcr.io/dokimi-io/orchestrator@{DIGEST}"
#: The orchestrator's image, for tests about another component in a source build.
ORCHESTRATOR = {"DOKIMI_ORCHESTRATOR_IMAGE": "orchestrator:dev"}
#: What submit writes for the orchestrator, for tests about reading another one back.
ORCHESTRATOR_IN_JOB = {
    "DOKIMI_ORCHESTRATOR_IMAGE": "orchestrator:dev",
    "DOKIMI_ORCHESTRATOR_IMAGE_SOURCE": "run-override",
}


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", DIGEST)
    monkeypatch.setattr(_image_digest, "IPERF2_IMAGE_DIGEST", DIGEST)


@pytest.fixture
def source_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", None)
    monkeypatch.setattr(_image_digest, "IPERF2_IMAGE_DIGEST", None)


# -- choosing ---------------------------------------------------------------------------


def test_every_component_is_named_and_has_an_environment_variable() -> None:
    assert sorted(COMPONENTS) == ["iperf2", "node-exporter", "orchestrator", "prometheus"]
    assert COMPONENTS["iperf2"].env_var == "DOKIMI_IPERF2_IMAGE"
    assert COMPONENTS["iperf2"].source_env_var == "DOKIMI_IPERF2_IMAGE_SOURCE"
    # The orchestrator is a component like the others; its earlier name stays.
    assert COMPONENTS["orchestrator"].env_var == "DOKIMI_ORCHESTRATOR_IMAGE"
    assert COMPONENTS["orchestrator"].env_aliases == ("DOKIMI_IMAGE",)


def test_a_released_build_runs_its_own_digest(released: None) -> None:
    assert resolve_run_images(env={}) == {
        "orchestrator": WorkloadImage(RELEASED_ORCHESTRATOR, ImageSource.RELEASE_DEFAULT),
        "iperf2": WorkloadImage(RELEASED, ImageSource.RELEASE_DEFAULT),
        "prometheus": WorkloadImage(PROMETHEUS_IMAGE, ImageSource.RELEASE_DEFAULT),
        "node-exporter": WorkloadImage(NODE_EXPORTER_IMAGE, ImageSource.RELEASE_DEFAULT),
    }


def test_the_monitoring_images_are_pinned_upstream_digests_even_in_a_source_build(
    source_build: None,
) -> None:
    for name in ("prometheus", "node-exporter"):
        image = resolve_component(COMPONENTS[name], env={})
        assert image is not None
        assert image.source is ImageSource.RELEASE_DEFAULT
        assert image.reference.startswith("quay.io/prometheus/")
        assert len(image.reference.partition("@sha256:")[2]) == 64


def test_the_monitoring_images_are_resolved_only_when_the_run_may_deploy_them(
    released: None,
) -> None:
    # The `existing` monitoring mode deploys neither, so neither is resolved, written or expected.
    images = resolve_run_images(env={}, monitoring=False)
    assert sorted(images) == ["iperf2", "orchestrator"]
    environment = {entry["name"]: entry["value"] for entry in job_environment(images)}
    assert images_from_job_environment(environment, monitoring=False) == images
    with pytest.raises(ConfigError, match="without an image for prometheus"):
        images_from_job_environment(environment)


def test_a_mirror_for_the_monitoring_images_is_an_installation_override(released: None) -> None:
    images = resolve_run_images(
        env={"DOKIMI_PROMETHEUS_IMAGE": "mirror.local/prometheus:v3.15.0"},
    )
    assert images["prometheus"] == WorkloadImage(
        "mirror.local/prometheus:v3.15.0", ImageSource.INSTALLATION_OVERRIDE
    )
    assert COMPONENTS["node-exporter"].env_var == "DOKIMI_NODE_EXPORTER_IMAGE"


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
        resolve_run_images(env=ORCHESTRATOR)


def test_a_source_build_runs_with_an_override(source_build: None) -> None:
    images = resolve_run_images(env={**ORCHESTRATOR, "DOKIMI_IPERF2_IMAGE": "iperf2:dev"})

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


def test_the_orchestrator_flag_is_an_alias_of_its_component() -> None:
    assert parse_image_options([], orchestrator_image="orchestrator:dev") == {
        "orchestrator": "orchestrator:dev"
    }
    assert parse_image_options(
        ["orchestrator=orchestrator:dev"], orchestrator_image="orchestrator:dev"
    ) == {"orchestrator": "orchestrator:dev"}


def test_the_orchestrator_flag_and_its_component_must_agree() -> None:
    with pytest.raises(ConfigError, match="name different images"):
        parse_image_options(["orchestrator=orchestrator:1"], orchestrator_image="orchestrator:2")


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
        "orchestrator": WorkloadImage("orchestrator:dev", ImageSource.RUN_OVERRIDE),
        "iperf2": WorkloadImage("mirror.local/iperf2:2.2.1", ImageSource.INSTALLATION_OVERRIDE),
        "prometheus": WorkloadImage(PROMETHEUS_IMAGE, ImageSource.RELEASE_DEFAULT),
        "node-exporter": WorkloadImage("node-exporter:dev", ImageSource.RUN_OVERRIDE),
    }
    environment = {entry["name"]: entry["value"] for entry in job_environment(images)}

    assert images_from_job_environment(environment) == images


def test_the_job_never_falls_back_to_a_default(released: None) -> None:
    # A Job without its images was not made by this CLI. Guessing a default inside the
    # cluster would run something the submitter never chose or saw.
    with pytest.raises(ConfigError, match="submitted without an image for iperf2"):
        images_from_job_environment(ORCHESTRATOR_IN_JOB)


def test_the_job_refuses_a_source_it_does_not_know() -> None:
    environment = {
        **ORCHESTRATOR_IN_JOB,
        "DOKIMI_IPERF2_IMAGE": "iperf2:dev",
        "DOKIMI_IPERF2_IMAGE_SOURCE": "guess",
    }

    with pytest.raises(ConfigError, match="not an image source"):
        images_from_job_environment(environment)
