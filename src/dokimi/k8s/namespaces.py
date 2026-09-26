"""Where a run's objects go: one namespace per test type, not per run.

[ADR-05-02]: all ``iperf2`` runs share one namespace and all IRTT runs another. That
isolates unrelated test types from each other without a namespace per run, which would
need cluster-level permission to create namespaces and would make every cleanup wait on
slow namespace deletion. The default name is derived from the test type; configuration
may override it.

The namespace is shared across runs, so it is never run-owned: this module only names
it. Creating it is an installation step, not something a run does ([ADR-05-13]):
``deploy/rbac.yaml`` creates the default one, and an override must already exist.
"""

from __future__ import annotations

import re

from dokimi.model.errors import ConfigError

__all__ = [
    "NAMESPACE_PREFIX",
    "InvalidNamespaceError",
    "namespace_for",
]

NAMESPACE_PREFIX = "dokimi-"

# RFC 1123 label: what Kubernetes requires of a namespace name.
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_LENGTH = 63


class InvalidNamespaceError(ConfigError, ValueError):
    """A namespace name Kubernetes would reject. A ``config_error`` (exit 3, ADR-01-11)."""


def namespace_for(test_type: str, override: str | None = None) -> str:
    """The namespace for *test_type*'s objects: *override* if given, else the default.

    >>> namespace_for("iperf2")
    'dokimi-iperf2'
    """
    if override is not None and override.strip():
        return _validated(override.strip(), "the configured namespace override")
    return _validated(f"{NAMESPACE_PREFIX}{test_type.lower()}", f"test type {test_type!r}")


def _validated(name: str, source: str) -> str:
    if len(name) > _MAX_LENGTH or not _DNS_LABEL.match(name):
        raise InvalidNamespaceError(
            f"{name!r}, derived from {source}, is not a valid namespace name: it must be at "
            f"most {_MAX_LENGTH} characters of lowercase letters, digits and '-', starting "
            "and ending with a letter or digit"
        )
    return name
