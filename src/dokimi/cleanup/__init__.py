"""Phase 11, and everything that removes a run after it.

A run that passed leaves nothing but its kept Prometheus. A run that didn't keeps its
evidence until someone removes it, without holding the cluster
(:mod:`~dokimi.cleanup.retention`). Its test pods go first, and are waited for, because a
pod can still be sending after it was deleted (:mod:`~dokimi.cleanup.test_pods`). And a
run can be removed from outside, by its id or by its age, without its own process or
anything it remembered (:mod:`~dokimi.cleanup.standalone`).
"""

from dokimi.cleanup.retention import QUIET_ERRORS, Retention, quiet_tests
from dokimi.cleanup.standalone import (
    Release,
    RunCleanup,
    clean_up_run,
    release_fetched_run,
    runs_older_than,
)
from dokimi.cleanup.test_pods import (
    POD_REMOVAL_TIMEOUT_SECONDS,
    TEST_POD_COMPONENTS,
    PodRemoval,
    remove_test_pods,
    test_pod_selector,
)

__all__ = [
    "POD_REMOVAL_TIMEOUT_SECONDS",
    "QUIET_ERRORS",
    "TEST_POD_COMPONENTS",
    "PodRemoval",
    "Release",
    "Retention",
    "RunCleanup",
    "clean_up_run",
    "quiet_tests",
    "release_fetched_run",
    "remove_test_pods",
    "runs_older_than",
    "test_pod_selector",
]
