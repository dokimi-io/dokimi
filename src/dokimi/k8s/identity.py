"""Run identity and the ownership labels every created object carries.

This is the one place the label keys and the ``managed-by`` value are spelled:
``app.kubernetes.io/managed-by: dokimi`` and ``dokimi.io/run-id``.
:mod:`dokimi.k8s.ownership` builds on the same constants.

Run ids and namespace names each have exactly one implementation, and this module
delegates to it rather than keeping a second copy:

* the ``run_id`` format, ``YYYYMMDD-HHMMSS-xxxxxx``, is
  :mod:`dokimi.run_identity`;
* the per-test-type namespace and its validation are :mod:`dokimi.k8s.namespaces`.

What this module keeps is the string-level API the submission path uses: a ``run_id``
that is about to become a label value and part of every object name, and the selectors
that find a run again from another process.
"""

from __future__ import annotations

from datetime import datetime

from dokimi import run_identity
from dokimi.k8s.namespaces import namespace_for
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

_EXAMPLE = "20260924-143012-k3f9x2"


def new_run_id(now: datetime | None = None) -> str:
    """Generate the run id for a run starting at *now* (see :func:`run_identity.new_run_id`)."""
    return str(run_identity.new_run_id(now))


def validate_run_id(run_id: str) -> str:
    """Return *run_id* if it is well formed.

    Reattaching takes a ``run_id`` from a human or a CI variable and puts it straight
    into a label selector, so it is checked here rather than trusted. A malformed one is
    a ``config_error``, not a failed lookup that looks like "your run has finished".

    Validation is strict: the value must match the pattern *and*
    encode a real calendar date and time. Surrounding whitespace is the only thing
    stripped; nothing else is normalised, so ``K3F9X2`` is refused rather than lowercased.
    """
    try:
        return str(run_identity.parse_run_id(run_id))
    except run_identity.InvalidRunIdError:
        raise ConfigError(
            f"malformed run id {run_id!r}. Expected YYYYMMDD-HHMMSS-xxxxxx — a UTC "
            f"timestamp, then six characters of [0-9a-z] — for example {_EXAMPLE!r}"
        ) from None


def ownership_labels(run_id: str, component: str | None = None) -> dict[str, str]:
    """The labels every object a run creates must carry."""
    labels = {MANAGED_BY_LABEL: MANAGED_BY, RUN_ID_LABEL: run_id}
    if component is not None:
        labels[COMPONENT_LABEL] = component
    return labels


def run_selector(run_id: str, component: str | None = None) -> str:
    """A label selector matching exactly one run's objects.

    Every delete and every lookup goes through a selector built here. That is the
    mechanism behind the rule that nothing unlabelled is ever touched: there is
    no code path that addresses a run's resources by name alone.
    """
    return ",".join(f"{key}={value}" for key, value in ownership_labels(run_id, component).items())


def default_namespace(test_type: TestType) -> str:
    """The namespace a test type's runs share.

    Per test type, not per run: a namespace per run would need cluster-level permission
    to create one on every invocation and would make cleanup wait on namespace deletion.
    """
    return namespace_for(test_type.value)
