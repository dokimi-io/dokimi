"""Workload deadlines: derived from the test, covering every wait, never retrying."""

from __future__ import annotations

import pytest

from dokimi.k8s.deadlines import (
    DeadlineBudget,
    DeadlineError,
    JobBudget,
    bound_job,
    bound_pod,
)
from dokimi.k8s.resources import Manifest


def budget(**overrides: float) -> DeadlineBudget:
    terms = {
        "test_duration": 10.0,
        "startup": 60.0,
        "warmup": 20.0,
        "direct_read": 5.0,
        "scrape_confirmation": 30.0,
    }
    return DeadlineBudget(**(terms | overrides))


def test_deadline_covers_the_test_and_every_wait_after_it() -> None:
    # 10 s of test is not enough: the pods must survive the direct read and the scrape
    # confirmation window too, or the deadline kills them during a wait (ADR-03-01).
    assert budget(safety_margin=0).active_deadline_seconds == 10 + 60 + 20 + 5 + 30


def test_deadline_covers_the_warm_up_pass() -> None:
    # ADR-04-04 runs a discarded warm-up pass by default. A deadline that ignored it
    # could expire during warm-up, before the measurement even starts (ADR-05-13).
    assert (
        budget(warmup=20).active_deadline_seconds - budget(warmup=0).active_deadline_seconds == 20
    )


def test_the_job_budget_covers_every_workload_and_the_phases_around_them() -> None:
    job = JobBudget(
        workloads=(budget(safety_margin=0), budget(safety_margin=0)),
        preflight=300,
        diagnostics=120,
        cleanup=120,
        safety_margin=0,
    )

    assert job.active_deadline_seconds == 2 * (10 + 60 + 20 + 5 + 30) + 300 + 120 + 120
    assert job.total > budget().total


def test_a_job_budget_needs_a_workload() -> None:
    with pytest.raises(DeadlineError):
        JobBudget(workloads=(), preflight=0, diagnostics=0, cleanup=0)


def test_deadline_grows_with_the_test_duration() -> None:
    assert (
        budget(test_duration=600).active_deadline_seconds
        - budget(test_duration=10).active_deadline_seconds
        == 590
    )


def test_deadline_rounds_up_to_whole_seconds() -> None:
    assert budget(test_duration=10.2, safety_margin=0).active_deadline_seconds == 126


@pytest.mark.parametrize(
    "overrides",
    [{"test_duration": 0}, {"startup": -1}, {"scrape_confirmation": float("nan")}],
)
def test_nonsense_terms_are_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(DeadlineError):
        budget(**overrides)


def pod() -> Manifest:
    return {"kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": []}}


def job() -> Manifest:
    return {"kind": "Job", "metadata": {"name": "j"}, "spec": {"template": {"spec": {}}}}


def test_bound_pod_sets_the_deadline_and_never_restarts() -> None:
    bounded = bound_pod(pod(), budget())

    assert bounded["spec"]["activeDeadlineSeconds"] == budget().active_deadline_seconds
    assert bounded["spec"]["restartPolicy"] == "Never"


def test_bound_job_never_retries() -> None:
    bounded = bound_job(job(), budget())

    assert bounded["spec"]["activeDeadlineSeconds"] == budget().active_deadline_seconds
    assert bounded["spec"]["backoffLimit"] == 0
    assert bounded["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_bound_job_refuses_retries_rather_than_overriding_them() -> None:
    retrying = job()
    retrying["spec"]["backoffLimit"] = 6

    with pytest.raises(DeadlineError, match="backoffLimit"):
        bound_job(retrying, budget())


def test_bounding_does_not_mutate_its_input() -> None:
    original = pod()
    bound_pod(original, budget())

    assert "activeDeadlineSeconds" not in original["spec"]


def test_wrong_kind_is_refused() -> None:
    with pytest.raises(DeadlineError, match="expected a Job"):
        bound_job(pod(), budget())
