"""Every way a test can end without a valid measurement carries its error category.

The category decides what an ``error`` points at, and which of a failed run's test pods
may be kept, so it is read from the exception, never from its message.
"""

from __future__ import annotations

import pytest

from dokimi.error_categories import ErrorCategory, category_of
from dokimi.execution.network_test import ToolFailedError
from dokimi.measurement import ExporterParseError, MetricsMissingError
from dokimi.testtypes.interface import Role
from dokimi.testtypes.iperf2 import UnexpectedTargetError
from dokimi.testtypes.lifecycle import (
    CompletionTimeoutError,
    ReadinessGateError,
    WorkloadLostError,
)


def test_the_categories_are_spelled_as_users_filter_on_them() -> None:
    assert [category.value for category in ErrorCategory] == [
        "scheduling",
        "image_pull",
        "tool_execution",
        "exporter_parse",
        "metrics_missing",
        "prometheus_unreachable",
        "timeout",
        "cleanup",
    ]


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (
            ReadinessGateError(
                "pair-001", {Role.SERVER: "x"}, {Role.SERVER: ErrorCategory.IMAGE_PULL}
            ),
            ErrorCategory.IMAGE_PULL,
        ),
        (ToolFailedError("pair-001: iperf2 exited 69"), ErrorCategory.TOOL_EXECUTION),
        (UnexpectedTargetError("pair-001 measured 10.9.9.9"), ErrorCategory.TOOL_EXECUTION),
        (WorkloadLostError("pair-001's server is gone"), ErrorCategory.TOOL_EXECUTION),
        (CompletionTimeoutError("pair-001 did not complete"), ErrorCategory.TIMEOUT),
        (MetricsMissingError("pair-001", "no result"), ErrorCategory.METRICS_MISSING),
        (ExporterParseError("pair-001", "not a number"), ErrorCategory.EXPORTER_PARSE),
    ],
    ids=lambda value: type(value).__name__,
)
def test_an_error_that_ends_a_test_carries_its_category(
    error: Exception, category: ErrorCategory
) -> None:
    assert category_of(error) is category


def test_an_error_nobody_anticipated_has_no_category() -> None:
    # Still an error, never a pass: it just has nothing more specific to say.
    assert category_of(RuntimeError("surprise")) is None
