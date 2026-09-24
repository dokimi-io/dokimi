"""Run identity and ownership labels (ADR-02-04, ADR-05-02)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from test_orchestrator.k8s.identity import (
    MANAGED_BY,
    MANAGED_BY_LABEL,
    RUN_ID_LABEL,
    default_namespace,
    new_run_id,
    ownership_labels,
    run_selector,
    validate_run_id,
)
from test_orchestrator.model import ConfigError, TestType


def test_run_id_is_sortable_by_start_time() -> None:
    # ADR-02-04 chose a timestamped form over a UUID precisely for this: the archive
    # orders naturally without parsing anything.
    earlier = new_run_id(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC))
    later = new_run_id(datetime(2026, 1, 1, 10, 0, 1, tzinfo=UTC))

    assert earlier < later


def test_run_id_is_unique_per_run() -> None:
    moment = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    generated = {new_run_id(moment) for _ in range(100)}

    assert len(generated) > 90, "the random suffix is not doing its job"


def test_run_id_is_a_valid_label_value() -> None:
    # A run id ends up in `test-orchestrator/run-id`, so it has to satisfy Kubernetes'
    # label value rules or nothing can be created at all.
    run_id = new_run_id()

    assert len(run_id) <= 63
    assert re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", run_id)


def test_naive_timestamps_are_read_as_utc() -> None:
    assert new_run_id(datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)).startswith("20260101T100000Z-")


@pytest.mark.parametrize(
    "malformed",
    ["", "not-a-run-id", "20260101T100000Z", "20260101T100000Z-XYZ123", "20260101-4f2a9c"],
)
def test_a_malformed_run_id_is_a_config_error(malformed: str) -> None:
    # Reattach puts this straight into a label selector. A bad one must not come back as
    # "no such run", which reads like the run finished.
    with pytest.raises(ConfigError, match="malformed run id"):
        validate_run_id(malformed)


def test_a_well_formed_run_id_survives_validation() -> None:
    assert validate_run_id("  20260101T100000Z-4f2a9c  ") == "20260101T100000Z-4f2a9c"


def test_ownership_labels_carry_the_two_adr_05_02_requires() -> None:
    labels = ownership_labels("20260101T100000Z-4f2a9c")

    assert labels[MANAGED_BY_LABEL] == MANAGED_BY
    assert labels[RUN_ID_LABEL] == "20260101T100000Z-4f2a9c"


def test_the_selector_scopes_to_one_run() -> None:
    selector = run_selector("20260101T100000Z-4f2a9c", "orchestrator")

    assert f"{RUN_ID_LABEL}=20260101T100000Z-4f2a9c" in selector
    assert f"{MANAGED_BY_LABEL}={MANAGED_BY}" in selector
    assert "app.kubernetes.io/component=orchestrator" in selector


def test_namespaces_are_per_test_type_not_per_run() -> None:
    first = default_namespace(TestType.IPERF2)
    second = default_namespace(TestType.IPERF2)

    assert first == second == "test-orchestrator-iperf2"
