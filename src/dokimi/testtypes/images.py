"""Test-workload images: which reference a test type's pods actually run.

[ADR-05-04] decides the strategy: one image per test type, built from a Dockerfile in
this repository, published to ``ghcr.io/dokimi-io/<image-name>`` ([ADR-01-10]), pinned by
digest, overridable per test type, and recorded in the run metadata. This is the
workload-image counterpart of :mod:`dokimi.images`, and it reuses that module's release
mechanism: the release workflow writes each digest into :mod:`dokimi._image_digest`.

Who chooses an override is [ADR-04-08]'s: :mod:`dokimi.run_images` resolves every image a
run deploys at submission, from ``--image`` and ``DOKIMI_<COMPONENT>_IMAGE`` on the
submitter's side, and passes the result into the Job. A plugin is handed the resolved
image. Nothing here reads the environment, because inside the Job the submitter's
environment does not exist.
"""

from __future__ import annotations

import re

from dokimi.images import ImageSource
from dokimi.testtypes.interface import WorkloadImage

__all__ = [
    "WORKLOAD_REGISTRY",
    "WorkloadImageError",
    "default_reference",
    "resolve_workload_image",
]

#: The registry and organisation every workload image lives under ([ADR-01-10]).
WORKLOAD_REGISTRY = "ghcr.io/dokimi-io"

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class WorkloadImageError(RuntimeError):
    """No usable workload image reference could be determined."""


def default_reference(image_name: str, baked_digest: str | None) -> str | None:
    """The released image of *image_name*, by digest, or ``None`` in a source checkout.

    Raises:
        WorkloadImageError: if the generated digest is malformed, which only a hand edit
            of :mod:`dokimi._image_digest` can cause.
    """
    if baked_digest is None:
        return None
    if not _DIGEST_PATTERN.match(baked_digest):
        raise WorkloadImageError(
            f"the generated {image_name} digest {baked_digest!r} is not a sha256 digest. "
            "This file is written by the release workflow and should not be edited by hand."
        )
    return f"{WORKLOAD_REGISTRY}/{image_name}@{baked_digest}"


def resolve_workload_image(
    image_name: str, baked_digest: str | None, override: str | None = None
) -> WorkloadImage:
    """The reference a test type's pods run: *override* if given, else the release default.

    For callers of the library that choose an image themselves. A run's images are
    resolved by :mod:`dokimi.run_images` instead, which knows which level an override
    came from.

    Raises:
        WorkloadImageError: in a source checkout with no override — there is no published
            image of this code, and a tag would name whatever it points at today.
    """
    if override is not None and override.strip():
        return WorkloadImage(override.strip(), ImageSource.RUN_OVERRIDE)
    reference = default_reference(image_name, baked_digest)
    if reference is None:
        raise WorkloadImageError(
            f"this is a development build with no published {image_name} image, so there is "
            "no default to run. Pass an explicit image reference for the test type."
        )
    return WorkloadImage(reference, ImageSource.RELEASE_DEFAULT)
