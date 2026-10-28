"""Why a test is an ``error`` rather than a ``pass`` or a ``fail``, as data.

An ``error`` says nothing about the network: the test was not validly measured. Its
category says what to look at instead, so that a flaky environment can be told apart from a
degrading network without reading a message. Every category is listed here, once, and each
exception that ends a test without a valid measurement carries one as its ``category``
attribute. Nothing parses a message to find it.

The values are part of the result files and of the tags in Robot's report, so they are
spelled exactly as users filter on them.
"""

from __future__ import annotations

import enum

__all__ = ["ErrorCategory", "category_of"]


class ErrorCategory(enum.StrEnum):
    """Every error category a test, or a run's cleanup, can have."""

    #: A test pod could not be placed on its node.
    SCHEDULING = "scheduling"
    #: A test pod could not get its image.
    IMAGE_PULL = "image_pull"
    #: The tool ran and measured nothing, measured the wrong path, or lost its pod.
    TOOL_EXECUTION = "tool_execution"
    #: An exporter served something that is not a result.
    EXPORTER_PARSE = "exporter_parse"
    #: An exporter served no result for the test.
    METRICS_MISSING = "metrics_missing"
    #: The run's Prometheus could not be queried.
    PROMETHEUS_UNREACHABLE = "prometheus_unreachable"
    #: The test did not finish in the time it was given.
    TIMEOUT = "timeout"
    #: Something the run created could not be removed.
    CLEANUP = "cleanup"


def category_of(error: BaseException) -> ErrorCategory | None:
    """The category *error* carries, or ``None`` for one that was not anticipated."""
    category = getattr(error, "category", None)
    return category if isinstance(category, ErrorCategory) else None
