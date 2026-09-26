"""The orchestrator image reference: resolution, override precedence and metadata.

ADR-05-08's requirements are easy to state and easy to lose: a released CLI submits the
digest of the image built from its own commit, an operator can override that reference,
and whatever was used ends up in the run metadata. These are the tests that keep those
three properties from quietly decaying into "whatever the default tag resolves to today".
"""

from __future__ import annotations

import pytest

from dokimi import images
from dokimi.images import (
    IMAGE_OVERRIDE_ENV_VAR,
    IMAGE_REPOSITORY,
    ImageResolutionError,
    ImageSource,
    OrchestratorImage,
    resolve_image,
)

DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "cd" * 32


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> str:
    """A build whose release baked in a digest, as the release workflow does."""
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", DIGEST)
    return f"{IMAGE_REPOSITORY}@{DIGEST}"


def test_released_build_defaults_to_its_own_digest(released: str) -> None:
    image = resolve_image(env={})

    assert image.reference == released
    assert image.source is ImageSource.RELEASE_DEFAULT
    assert image.is_pinned_by_digest


def test_development_checkout_has_no_default() -> None:
    # The checked-in constant is None, and inventing a tag here would submit whatever
    # `:latest` happens to be rather than a build of this code.
    with pytest.raises(ImageResolutionError, match="development build"):
        resolve_image(env={})


def test_installation_override_beats_the_baked_in_default(released: str) -> None:
    image = resolve_image(env={IMAGE_OVERRIDE_ENV_VAR: "registry.internal/orchestrator:1.2.3"})

    assert image.reference == "registry.internal/orchestrator:1.2.3"
    assert image.source is ImageSource.INSTALLATION_OVERRIDE


def test_run_override_beats_the_installation_override(released: str) -> None:
    image = resolve_image(
        "registry.internal/orchestrator@" + OTHER_DIGEST,
        env={IMAGE_OVERRIDE_ENV_VAR: "registry.internal/orchestrator:1.2.3"},
    )

    assert image.reference == "registry.internal/orchestrator@" + OTHER_DIGEST
    assert image.source is ImageSource.RUN_OVERRIDE
    assert image.digest == OTHER_DIGEST


def test_an_override_can_rescue_a_development_build() -> None:
    image = resolve_image("registry.internal/orchestrator@" + DIGEST, env={})

    assert image.source is ImageSource.RUN_OVERRIDE


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_overrides_are_the_same_as_unset(blank: str, released: str) -> None:
    assert resolve_image(blank, env={IMAGE_OVERRIDE_ENV_VAR: blank}).reference == released


def test_a_tagged_override_is_honoured_but_recorded_as_unpinned() -> None:
    # An air-gapped mirror may not expose digests. The reference is used as given; what
    # the run must not do is claim a pin it does not have.
    image = resolve_image("registry.internal/orchestrator:1.2.3", env={})

    assert image.digest is None
    assert not image.is_pinned_by_digest
    assert image.run_metadata()["orchestrator_image_digest"] == ""


def test_a_malformed_generated_digest_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "ORCHESTRATOR_IMAGE_DIGEST", "not-a-digest")

    with pytest.raises(ImageResolutionError, match="not a sha256 digest"):
        resolve_image(env={})


def test_run_metadata_records_reference_digest_and_source(released: str) -> None:
    metadata = resolve_image(env={}).run_metadata()

    assert metadata == {
        "orchestrator_image": released,
        "orchestrator_image_digest": DIGEST,
        "orchestrator_image_source": "release-default",
    }


@pytest.mark.parametrize(
    "reference",
    [
        "ghcr.io/dokimi-io/orchestrator:1.0.0",
        "ghcr.io/dokimi-io/orchestrator@sha256:short",
        "ghcr.io/dokimi-io/orchestrator@sha512:" + "ab" * 32,
        "ghcr.io/dokimi-io/orchestrator@" + "AB" * 32,
    ],
)
def test_references_that_do_not_pin_a_digest(reference: str) -> None:
    assert OrchestratorImage(reference, ImageSource.RUN_OVERRIDE).digest is None
