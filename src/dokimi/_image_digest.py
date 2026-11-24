"""Generated at release time — do not edit by hand.

The CLI ships with the digest of the orchestrator image built from its own commit, and
every test-workload image is pinned by digest the same way. The
release workflow (``.github/workflows/release.yml``) builds and pushes the images,
rewrites this file with the digests the registry returned, and only then builds the
distribution — so a released CLI and the images it runs always come from one commit.

In a source checkout every value is ``None``: this build was never published, there is
no digest to run, and :mod:`dokimi.images` and :mod:`dokimi.testtypes.images` say so
rather than inventing a tag that might resolve to something else tomorrow.
"""

from __future__ import annotations

ORCHESTRATOR_IMAGE_DIGEST: str | None = None
IPERF2_IMAGE_DIGEST: str | None = None
