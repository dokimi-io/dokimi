"""The orchestrator's own container image: what reference a run actually submits.

[ADR-01-06] makes the orchestrator a one-shot in-cluster Job, so a run *is* a reference
to an image. [ADR-05-08] decides what that reference is — ``ghcr.io/test-orchestrator/
orchestrator``, pinned by digest, the digest of the image built from the CLI's own
commit, overridable for air-gapped or policy-constrained clusters, and recorded in the
run metadata.

ADR-05-08 left two things as follow-up work. This module settles both:

* **Release mechanism** — a generated constants file, :mod:`test_orchestrator._image_digest`,
  rewritten by the release workflow between pushing the image and building the
  distribution. Chosen over a build-time template or a lockfile-adjacent manifest because
  it needs no packaging hook and is readable by the same import machinery as everything
  else.
* **Override interface** — ``--orchestrator-image`` for one run, the
  ``TEST_ORCHESTRATOR_IMAGE`` environment variable for one installation, the baked-in
  digest otherwise. Two levels and no config file, deliberately: [ADR-04-02]
  (configuration sources and precedence) is still ``proposed``, and Milestone-01 must not
  implement a precedence chain a later decision would overturn.

An override is honoured as given, including a tag rather than a digest — an air-gapped
mirror may not expose digests the same way. What the run *cannot* do is stay quiet about
it: :meth:`OrchestratorImage.run_metadata` records the reference, the digest if there is
one, and where the reference came from, so a measurement is always attributable to a
specific build ([ADR-05-04], [ADR-05-08]).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from test_orchestrator._image_digest import ORCHESTRATOR_IMAGE_DIGEST

__all__ = [
    "IMAGE_OVERRIDE_ENV_VAR",
    "IMAGE_REPOSITORY",
    "ImageResolutionError",
    "ImageSource",
    "OrchestratorImage",
    "default_reference",
    "resolve_image",
]

#: Registry and repository, inherited from ADR-05-04 rather than decided again here.
IMAGE_REPOSITORY = "ghcr.io/test-orchestrator/orchestrator"

#: Per-installation override. The per-run override is a CLI argument, not an env var.
IMAGE_OVERRIDE_ENV_VAR = "TEST_ORCHESTRATOR_IMAGE"

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ImageResolutionError(RuntimeError):
    """No usable image reference could be determined."""


class ImageSource(StrEnum):
    """Where a resolved reference came from. Recorded in the run metadata."""

    RUN_OVERRIDE = "run-override"
    INSTALLATION_OVERRIDE = "installation-override"
    RELEASE_DEFAULT = "release-default"


@dataclass(frozen=True)
class OrchestratorImage:
    """A resolved image reference and its provenance."""

    reference: str
    source: ImageSource

    @property
    def digest(self) -> str | None:
        """The ``sha256:...`` digest this reference pins, or ``None`` if it pins a tag."""
        _, separator, digest = self.reference.partition("@")
        if not separator or not _DIGEST_PATTERN.match(digest):
            return None
        return digest

    @property
    def is_pinned_by_digest(self) -> bool:
        return self.digest is not None

    def run_metadata(self) -> dict[str, str]:
        """The run-metadata fields this image contributes.

        M1-05 (#40) merges these into the run's metadata and M1-14 (#49) writes them to
        the result artifact, next to the test-workload digests ADR-05-04 already puts
        there. Keys are stable and always present — an empty digest is a fact worth
        recording, not a field to omit.
        """
        return {
            "orchestrator_image": self.reference,
            "orchestrator_image_digest": self.digest or "",
            "orchestrator_image_source": str(self.source),
        }


def default_reference() -> str | None:
    """The digest-pinned reference baked in at release time, or ``None`` in a checkout."""
    digest = ORCHESTRATOR_IMAGE_DIGEST
    if digest is None:
        return None
    if not _DIGEST_PATTERN.match(digest):
        raise ImageResolutionError(
            f"the generated image digest {digest!r} is not a sha256 digest. "
            "This file is written by the release workflow and should not be edited by hand."
        )
    return f"{IMAGE_REPOSITORY}@{digest}"


def resolve_image(
    override: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> OrchestratorImage:
    """Resolve the image reference for a run.

    Precedence: *override* (one run) beats ``TEST_ORCHESTRATOR_IMAGE`` (one installation)
    beats the digest baked in at release time.

    Raises:
        ImageResolutionError: if nothing is baked in and nothing was overridden — the
            case of a development checkout, where submitting a Job needs an explicit
            image because there is no published build of this code.
    """
    environment = os.environ if env is None else env

    run_override = _clean(override)
    if run_override is not None:
        return OrchestratorImage(run_override, ImageSource.RUN_OVERRIDE)

    installation_override = _clean(environment.get(IMAGE_OVERRIDE_ENV_VAR))
    if installation_override is not None:
        return OrchestratorImage(installation_override, ImageSource.INSTALLATION_OVERRIDE)

    baked_in = default_reference()
    if baked_in is not None:
        return OrchestratorImage(baked_in, ImageSource.RELEASE_DEFAULT)

    raise ImageResolutionError(
        "this is a development build with no published orchestrator image, so there is "
        f"no default to submit. Pass an explicit reference or set {IMAGE_OVERRIDE_ENV_VAR}."
    )


def _clean(value: str | None) -> str | None:
    """Normalise an override: blank and whitespace-only are the same as unset."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
