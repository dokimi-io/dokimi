"""The manifests a submission creates carry the properties the ADRs require.

Several of these are the kind of thing that keeps working while being wrong — a Job with
a default `backoffLimit` runs fine and silently re-runs the matrix on a `fail`.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from dokimi.images import ImageSource
from dokimi.k8s.deadlines import job_budget_for, workload_budget_for
from dokimi.k8s.identity import MANAGED_BY_LABEL, RUN_ID_LABEL
from dokimi.k8s.manifests import (
    DEFINITION_MOUNT_PATH,
    bind_owner,
    job_deadline_seconds,
    render_manifests,
    resource_name,
)
from dokimi.k8s.ownership import CLAIMED_AT_ANNOTATION, claim_of
from dokimi.model import (
    Bandwidth,
    Comparison,
    Criterion,
    Duration,
    Metric,
    Protocol,
    TestDefinition,
    TestType,
    Unit,
)
from dokimi.testtypes.interface import WorkloadImage

RUN_ID = "20260101-100000-4f2a9c"
IMAGE = "ghcr.io/dokimi-io/orchestrator@sha256:" + "ab" * 32
#: The images a run deploys, as ``submit`` resolves them (ADR-04-08).
RUN_IMAGES = {
    "iperf2": WorkloadImage(
        "ghcr.io/dokimi-io/iperf2@sha256:" + "cd" * 32, ImageSource.RELEASE_DEFAULT
    )
}

#: Kubernetes object names: DNS-1123 labels, which forbid the uppercase a run id has.
DNS_1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


@pytest.fixture
def definition() -> TestDefinition:
    return TestDefinition(
        name="worker-01-to-worker-02-udp",
        source_node="worker-01",
        target_node="worker-02",
        test_type=TestType.IPERF2,
        protocol=Protocol.UDP,
        duration=Duration.parse("30s"),
        target_bandwidth=Bandwidth.parse("1G"),
        criteria=(
            Criterion(
                metric=Metric.THROUGHPUT,
                comparison=Comparison.AT_LEAST,
                threshold=Decimal(950),
                unit=Unit.MEGABITS_PER_SECOND,
            ),
        ),
    )


@pytest.fixture
def manifests(definition: TestDefinition) -> list[dict[str, Any]]:
    return render_manifests(
        definition,
        source="apiVersion: dokimi.io/v1alpha1\n",
        source_suffix=".yaml",
        run_id=RUN_ID,
        image=IMAGE,
        images=RUN_IMAGES,
        cluster="test-cluster",
        namespace="dokimi-iperf2",
    )


def _job(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    return next(m for m in manifests if m["kind"] == "Job")


def _container(manifests: list[dict[str, Any]]) -> dict[str, Any]:
    spec: dict[str, Any] = _job(manifests)["spec"]["template"]["spec"]["containers"][0]
    return spec


def test_the_owner_object_comes_first(manifests: list[dict[str, Any]]) -> None:
    # Nothing can reference an owner that does not exist yet, and an owner created last
    # would leave a window where a crash orphans everything.
    assert [m["kind"] for m in manifests] == ["ConfigMap", "ConfigMap", "Job"]
    assert manifests[0]["metadata"]["name"].endswith("-owner")


def test_everything_but_the_owner_points_at_the_owner(manifests: list[dict[str, Any]]) -> None:
    owner, *rest = manifests

    assert "ownerReferences" not in owner["metadata"]
    for manifest in rest:
        reference = manifest["metadata"]["ownerReferences"][0]
        assert reference["name"] == owner["metadata"]["name"]
        assert reference["kind"] == "ConfigMap"
        # ADR-05-13: unset, so deleting the owner never needs rights on its finalizers.
        assert "blockOwnerDeletion" not in reference


def test_the_owner_is_named_after_the_run_and_carries_its_identity(
    manifests: list[dict[str, Any]],
) -> None:
    # ADR-05-13: one owner, `run-<id>-owner`, with the run's id and creation time.
    owner = manifests[0]

    assert owner["metadata"]["name"] == f"run-{RUN_ID}-owner"
    assert owner["data"]["run_id"] == RUN_ID
    assert "created_at" in owner["data"]


def test_a_submission_is_never_evidence_of_an_active_run(
    manifests: list[dict[str, Any]],
) -> None:
    # The claim is written by the Job in phase 4, never at submission, so two submissions
    # made at once cannot refuse each other (ADR-05-10).
    owner = manifests[0]

    assert CLAIMED_AT_ANNOTATION not in (owner["metadata"].get("annotations") or {})
    assert claim_of(owner).claimed_at is None


def test_the_owner_uid_is_bound_after_creation(manifests: list[dict[str, Any]]) -> None:
    # The uid does not exist until the API server assigns it, so the rendered manifest
    # carries a placeholder and submission fills it in.
    job = _job(manifests)
    assert job["metadata"]["ownerReferences"][0]["uid"] == ""

    bound = bind_owner(job, "uid-from-the-api-server")

    assert bound["metadata"]["ownerReferences"][0]["uid"] == "uid-from-the-api-server"
    # A copy: the rendered manifest keeps its placeholder, so rendering it again after a
    # submission prints the same thing it printed before.
    assert job["metadata"]["ownerReferences"][0]["uid"] == ""


def test_the_job_never_retries(manifests: list[dict[str, Any]]) -> None:
    # ADR-01-08: the exit code is the verdict. A retry would re-run the whole matrix on
    # a plain `fail`, against ADR-05-05's rule that a fail is never re-rolled.
    job = _job(manifests)

    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_the_job_carries_a_self_limiting_deadline(
    manifests: list[dict[str, Any]], definition: TestDefinition
) -> None:
    # ADR-05-02: labels make cleanup possible, deadlines make it unnecessary.
    job = _job(manifests)

    assert job["spec"]["activeDeadlineSeconds"] == job_deadline_seconds(definition)
    assert job["spec"]["activeDeadlineSeconds"] > int(definition.duration.seconds)


def test_the_job_outlives_any_workload_it_runs(definition: TestDefinition) -> None:
    # ADR-05-13: the Job deadline is also when the claim expires, which is only safe
    # because by then every workload has hit its own, shorter deadline.
    workload = workload_budget_for(definition)

    assert job_budget_for(definition).total > workload.total
    assert workload.warmup > 0


def test_every_object_carries_the_ownership_labels(manifests: list[dict[str, Any]]) -> None:
    for manifest in manifests:
        labels = manifest["metadata"]["labels"]
        assert labels[MANAGED_BY_LABEL] == "dokimi"
        assert labels[RUN_ID_LABEL] == RUN_ID


def test_the_pod_carries_them_too(manifests: list[dict[str, Any]]) -> None:
    # Reattach finds the *pod* by selector, so labels on the Job alone are not enough.
    labels = _job(manifests)["spec"]["template"]["metadata"]["labels"]

    assert labels[RUN_ID_LABEL] == RUN_ID


def test_object_names_are_valid_and_derived_from_the_run_id(
    manifests: list[dict[str, Any]],
) -> None:
    for manifest in manifests:
        name = manifest["metadata"]["name"]
        assert DNS_1123.match(name), f"{name} is not a valid object name"
        assert RUN_ID in name


def test_the_definition_travels_with_the_run(
    manifests: list[dict[str, Any]], definition: TestDefinition
) -> None:
    # The Job reads the document from a ConfigMap, not from the submitter's disk, which
    # is what lets a reattaching process on another machine need nothing local.
    definition_map = manifests[1]
    assert definition_map["data"] == {"definition.yaml": "apiVersion: dokimi.io/v1alpha1\n"}

    assert _container(manifests)["args"] == [
        "execute",
        "--definition",
        f"{DEFINITION_MOUNT_PATH}/definition.yaml",
        "--run-id",
        RUN_ID,
        "--namespace",
        "dokimi-iperf2",
        "--deadline-seconds",
        str(job_deadline_seconds(definition)),
    ]


def test_a_preflight_is_the_same_job_told_to_stop_after_phase_4(
    definition: TestDefinition, manifests: list[dict[str, Any]]
) -> None:
    # `dokimi preflight`: one implementation, so one Job, and one argument more.
    preflight = render_manifests(
        definition,
        source="apiVersion: dokimi.io/v1alpha1\n",
        source_suffix=".yaml",
        run_id=RUN_ID,
        image=IMAGE,
        images=RUN_IMAGES,
        namespace="dokimi-iperf2",
        cluster="test-cluster",
        preflight_only=True,
    )

    assert _container(preflight)["args"] == [*_container(manifests)["args"], "--preflight-only"]
    assert preflight[1] == manifests[1]


def test_a_robot_suite_stays_a_robot_suite(definition: TestDefinition) -> None:
    # The in-cluster parse picks its format from the suffix exactly as the local one does.
    manifests = render_manifests(
        definition,
        source="*** Settings ***\n",
        source_suffix=".robot",
        run_id=RUN_ID,
        image=IMAGE,
        images=RUN_IMAGES,
        cluster="test-cluster",
        namespace="dokimi-iperf2",
    )

    assert "definition.robot" in manifests[1]["data"]
    assert f"{DEFINITION_MOUNT_PATH}/definition.robot" in _container(manifests)["args"]


def test_the_run_is_not_root(manifests: list[dict[str, Any]]) -> None:
    pod = _job(manifests)["spec"]["template"]["spec"]
    container = _container(manifests)

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_robot_has_somewhere_to_write(manifests: list[dict[str, Any]]) -> None:
    # A read-only root filesystem and phase 9a's output.xml are only compatible because
    # of this volume.
    mounts = {mount["mountPath"] for mount in _container(manifests)["volumeMounts"]}

    assert "/work" in mounts


def test_the_image_is_submitted_exactly_as_resolved(manifests: list[dict[str, Any]]) -> None:
    assert _container(manifests)["image"] == IMAGE


def test_the_job_is_given_the_images_the_run_deploys(manifests: list[dict[str, Any]]) -> None:
    # Resolved on the submitter's side, whose environment the Job never sees (ADR-04-08).
    env = {entry["name"]: entry["value"] for entry in _container(manifests)["env"]}

    assert env["DOKIMI_IPERF2_IMAGE"] == RUN_IMAGES["iperf2"].reference
    assert env["DOKIMI_IPERF2_IMAGE_SOURCE"] == "release-default"


def test_the_job_is_given_the_clusters_name(manifests: list[dict[str, Any]]) -> None:
    # ADR-02-07: given at submission, and the Job uses exactly that.
    env = {entry["name"]: entry["value"] for entry in _container(manifests)["env"]}

    assert env["DOKIMI_CLUSTER"] == "test-cluster"


def test_a_service_account_is_only_set_when_asked_for(definition: TestDefinition) -> None:
    without = render_manifests(
        definition,
        source="",
        source_suffix=".yaml",
        run_id=RUN_ID,
        image=IMAGE,
        images=RUN_IMAGES,
        cluster="test-cluster",
        namespace="ns",
    )
    with_account = render_manifests(
        definition,
        source="",
        source_suffix=".yaml",
        run_id=RUN_ID,
        image=IMAGE,
        images=RUN_IMAGES,
        cluster="test-cluster",
        namespace="ns",
        service_account="runner",
    )

    assert "serviceAccountName" not in _job(without)["spec"]["template"]["spec"]
    assert _job(with_account)["spec"]["template"]["spec"]["serviceAccountName"] == "runner"


def test_resource_names_use_the_run_id_verbatim() -> None:
    # ADR-02-08's format is already a valid object-name segment, so an object name and a
    # label spell the same run the same way. The previous format needed lowercasing here.
    assert resource_name(RUN_ID) == f"run-{RUN_ID}"
