"""The orchestrator's own container image: what reference a run actually submits.

The orchestrator is a one-shot in-cluster Job, so a run *is* a reference to an image.
That reference is ``ghcr.io/dokimi-io/orchestrator``, pinned by digest, the digest of the
image built from the CLI's own commit, overridable for air-gapped or policy-constrained
clusters, and recorded in the run metadata.

Two things about it are settled here:

* **Release mechanism** — a generated constants file, :mod:`dokimi._image_digest`,
  rewritten by the release workflow between pushing the image and building the
  distribution. Chosen over a build-time template or a lockfile-adjacent manifest because
  it needs no packaging hook and is readable by the same import machinery as everything
  else.
* **Override interface** — the orchestrator is one component among
  the images a run uses, chosen like the others in :mod:`dokimi.run_images`:
  ``--image orchestrator=<reference>`` for one run, ``DOKIMI_ORCHESTRATOR_IMAGE`` for
  one installation, the baked-in digest otherwise. ``--orchestrator-image`` and
  ``DOKIMI_IMAGE`` are kept as aliases. Two levels and no config file, deliberately:
  configuration sources and their precedence are not decided yet.

An override is honoured as given, including a tag rather than a digest — an air-gapped
mirror may not expose digests the same way. What the run *cannot* do is stay quiet about
it: :meth:`OrchestratorImage.run_metadata` records the reference, the digest if there is
one, and where the reference came from, so a measurement is always attributable to a
specific build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from dokimi._image_digest import ORCHESTRATOR_IMAGE_DIGEST

__all__ = [
    "IMAGE_REPOSITORY",
    "ImageResolutionError",
    "ImageSource",
    "OrchestratorImage",
    "default_reference",
]

#: Registry and organisation are the test-workload images' own, not decided again here.
IMAGE_REPOSITORY = "ghcr.io/dokimi-io/orchestrator"

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
        the result artifact, next to the test-workload digests already recorded
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
