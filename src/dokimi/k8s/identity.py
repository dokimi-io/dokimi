"""Run identity and the ownership labels every created object carries.

The ``run_id`` format is ADR-02-08's — ``YYYYMMDD-HHMMSS-xxxxxx``, a UTC timestamp to the
second and six characters of ``[0-9a-z]`` — the same format M1-05 (#28) implements, so
the two agree on the join key of the whole architecture before either merges. ADR-02-08
refines ADR-02-04's "sortable ``<timestamp>-<short-random>``, not a UUID"; it is still
``proposed``, and this module changes with it if its deciders do. ADR-05-02
fixes the labels: ``app.kubernetes.io/managed-by`` and ``dokimi.io/run-id`` at
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

from dokimi.model import ConfigError, TestType

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
RUN_ID_LABEL = "dokimi.io/run-id"
COMPONENT_LABEL = "app.kubernetes.io/component"

MANAGED_BY = "dokimi"

#: ``YYYYMMDD-HHMMSS-xxxxxx`` (ADR-02-08): 20260924-143012-k3f9x2.
#:
#: Fixed width, so plain string order is chronological order. Lowercase letters, digits
#: and hyphens only, so one spelling is valid as a label value, as part of an object name,
#: as a Prometheus label value and as a CSV cell, with no conversion in any of them.
_RUN_ID = re.compile(r"^(?P<timestamp>\d{8}-\d{6})-(?P<suffix>[0-9a-z]{6})$")
_TIMESTAMP = "%Y%m%d-%H%M%S"
_SUFFIX_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_SUFFIX_LENGTH = 6
_EXAMPLE = "20260924-143012-k3f9x2"


def new_run_id(now: datetime | None = None) -> str:
    """Generate the run id for a run starting at *now*.

    A timezone-naive *now* is refused rather than guessed at (ADR-02-08): read as local
    time, it would put a run started in Berlin an hour or two out of order against one
    started in London, and the format exists to order runs.
    """
    moment = datetime.now(UTC) if now is None else now
    if moment.tzinfo is None:
        raise ValueError("new_run_id needs a timezone-aware datetime")
    timestamp = moment.astimezone(UTC).strftime(_TIMESTAMP)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))
    return f"{timestamp}-{suffix}"


def validate_run_id(run_id: str) -> str:
    """Return *run_id* if it is well formed.

    Reattaching takes a ``run_id`` from a human or a CI variable and puts it straight
    into a label selector, so it is checked here rather than trusted. A malformed one is
    a ``config_error``, not a failed lookup that looks like "your run has finished".

    Validation is strict, as ADR-02-08 requires: the value must match the pattern *and*
    encode a real calendar date and time. Surrounding whitespace is the only thing
    stripped; nothing else is normalised, so ``K3F9X2`` is refused rather than lowercased.
    """
    candidate = run_id.strip()
    match = _RUN_ID.match(candidate)
    if match is not None:
        try:
            datetime.strptime(match["timestamp"], _TIMESTAMP)
        except ValueError:
            match = None
    if match is None:
        raise ConfigError(
            f"malformed run id {run_id!r}. Expected YYYYMMDD-HHMMSS-xxxxxx — a UTC "
            f"timestamp, then six characters of [0-9a-z] — for example {_EXAMPLE!r} "
            "(ADR-02-08)"
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
    return f"dokimi-{test_type.value}"
