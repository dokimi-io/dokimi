"""Errors raised while a test definition is being read.

``ConfigError`` is the model's expression of the ``config_error`` category: an
invalid *request*, detected before anything is deployed. It is deliberately the only
error this package raises — a malformed definition must never reach the cluster, and
that detection happens in phase 2 with no cluster interaction at all.

The full outcome taxonomy (``pass``/``fail``/``error``/``config_error``/``skipped``) and
its mapping onto exit codes belong to M1-16, not here. This is the one category
phase 2 can produce.
"""

from __future__ import annotations

__all__ = ["ConfigError"]


class ConfigError(Exception):
    """The test definition is invalid.

    Raised before any cluster interaction. The message is user-facing: it names the field
    and what was wrong with it, because the person reading it is the person who wrote the
    definition.
    """
