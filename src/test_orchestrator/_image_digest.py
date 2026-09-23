"""Generated at release time — do not edit by hand.

ADR-05-08 requires the CLI to ship with the digest of the orchestrator image built from
its own commit. The release workflow (``.github/workflows/release.yml``) builds and
pushes the image, rewrites this file with the digest the registry returned, and only
then builds the distribution — so a released CLI and its image always come from one
commit and one ``uv.lock``.

In a source checkout the value is ``None``: this build was never published, there is no
digest to submit, and :mod:`test_orchestrator.images` says so rather than inventing a
tag that might resolve to something else tomorrow.
"""

from __future__ import annotations

ORCHESTRATOR_IMAGE_DIGEST: str | None = None
