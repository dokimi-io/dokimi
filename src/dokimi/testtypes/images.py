"""Test-workload images: which reference a test type's pods actually run.

[ADR-05-04] decides the strategy: one image per test type, built from a Dockerfile in
this repository, published to ``ghcr.io/dokimi-io/<image-name>`` ([ADR-01-10]), pinned by
digest, overridable per test type, and recorded in the run metadata. This is the
workload-image counterpart of :mod:`dokimi.images`, and it reuses that module's release
mechanism: the release workflow writes each digest into :mod:`dokimi._image_digest`.

The override is a library argument and nothing else. [ADR-04-06] makes the test document
Milestone-01's only configuration source, so there is deliberately no environment
variable here: the pods are created inside the Job, where the submitter's environment
does not reach, and a variable that silently did nothing would be worse than none.
"""

from __future__ import annotations

import re

from dokimi.testtypes.interface import ImageSource, WorkloadImage

__all__ = ["WORKLOAD_REGISTRY", "WorkloadImageError", "resolve_workload_image"]

#: The registry and organisation every workload image lives under ([ADR-01-10]).
WORKLOAD_REGISTRY = "ghcr.io/dokimi-io"

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class WorkloadImageError(RuntimeError):
    """No usable workload image reference could be determined."""


def resolve_workload_image(
    image_name: str, baked_digest: str | None, override: str | None = None
) -> WorkloadImage:
    """The reference a test type's pods run: *override* if given, else the release default.

    Raises:
        WorkloadImageError: in a source checkout with no override — there is no published
            image of this code, and a tag would name whatever it points at today.
    """
    if override is not None and override.strip():
        return WorkloadImage(override.strip(), ImageSource.OVERRIDE)
    if baked_digest is None:
        raise WorkloadImageError(
            f"this is a development build with no published {image_name} image, so there is "
            "no default to run. Pass an explicit image reference for the test type."
        )
    if not _DIGEST_PATTERN.match(baked_digest):
        raise WorkloadImageError(
            f"the generated {image_name} digest {baked_digest!r} is not a sha256 digest. "
            "This file is written by the release workflow and should not be edited by hand."
        )
    return WorkloadImage(
        f"{WORKLOAD_REGISTRY}/{image_name}@{baked_digest}", ImageSource.RELEASE_DEFAULT
    )
