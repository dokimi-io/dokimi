"""Run identity and the ownership labels every created object carries.

ADR-02-04 fixes the ``run_id`` format — a sortable ``<timestamp>-<short-random>``, not a
UUID, so it stays readable in a log line and orders naturally in the archive. ADR-05-02
fixes the labels: ``app.kubernetes.io/managed-by`` and ``test-orchestrator/run-id`` at
minimum, and nothing without them is ever modified or deleted.

Both decisions are accepted, so this module implements them rather than inventing
anything. M1-05 (#40) owns run identity and ownership as a subject and will extend what
is here — the ``test-id`` label, the placeholder owner object's full lifecycle, run
exclusivity. What M1-04 needs, and all it defines, is enough identity to submit a Job and
to find that Job again later from a different process on a different machine.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

from test_orchestrator.model import ConfigError, TestType

__all__ = [
    "MANAGED_BY",
    "MANAGED_BY_LABEL",
    "RUN_ID_LABEL",
    "COMPONENT_LABEL",
    "default_namespace",
    "new_run_id",
    "ownership_labels",
    "run_selector",
    "validate_run_id",
]

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
RUN_ID_LABEL = "test-orchestrator/run-id"
COMPONENT_LABEL = "app.kubernetes.io/component"

MANAGED_BY = "test-orchestrator"

#: ``<timestamp>-<short-random>``: 20250924T101530Z-4f2a9c. Sortable as plain text.
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")


def new_run_id(now: datetime | None = None) -> str:
    """Generate a run identifier for a run starting at *now* (UTC)."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def validate_run_id(run_id: str) -> str:
    """Return *run_id* if it is well formed.

    Reattaching takes a ``run_id`` from a human or a CI variable and puts it straight
    into a label selector, so it is checked here rather than trusted. A malformed one is
    a ``config_error``, not a failed lookup that looks like "your run has finished".
    """
    candidate = run_id.strip()
    if not _RUN_ID.match(candidate):
        raise ConfigError(
            f"malformed run id {run_id!r}. Expected <timestamp>-<short-random>, "
            "for example '20250924T101530Z-4f2a9c' (ADR-02-04)"
        )
    return candidate


def ownership_labels(run_id: str, component: str | None = None) -> dict[str, str]:
    """The labels every object a run creates must carry (ADR-05-02)."""
    labels = {MANAGED_BY_LABEL: MANAGED_BY, RUN_ID_LABEL: run_id}
    if component is not None:
        labels[COMPONENT_LABEL] = component
    return labels


def run_selector(run_id: str, component: str | None = None) -> str:
    """A label selector matching exactly one run's objects.

    Every delete and every lookup goes through a selector built here. That is the
    mechanism behind ADR-05-02's rule that nothing unlabelled is ever touched: there is
    no code path that addresses a run's resources by name alone.
    """
    return ",".join(f"{key}={value}" for key, value in ownership_labels(run_id, component).items())


def default_namespace(test_type: TestType) -> str:
    """The namespace a test type's runs share (ADR-05-02).

    Per test type, not per run: a namespace per run would need cluster-level permission
    to create one on every invocation and would make cleanup wait on namespace deletion.
    """
    return f"test-orchestrator-{test_type.value}"
