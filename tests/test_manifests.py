"""The manifests a submission creates carry the properties the ADRs require.

Several of these are the kind of thing that keeps working while being wrong — a Job with
a default `backoffLimit` runs fine and silently re-runs the matrix on a `fail`.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from test_orchestrator.k8s.identity import MANAGED_BY_LABEL, RUN_ID_LABEL
from test_orchestrator.k8s.manifests import (
    DEFINITION_MOUNT_PATH,
    bind_owner,
    deadline_seconds,
    namespace_manifest,
    render_manifests,
    resource_name,
)
from test_orchestrator.model import (
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

RUN_ID = "20260101-100000-4f2a9c"
IMAGE = "ghcr.io/test-orchestrator/orchestrator@sha256:" + "ab" * 32

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
        source="apiVersion: test-orchestrator/v1alpha1\n",
        source_suffix=".yaml",
        run_id=RUN_ID,
        image=IMAGE,
        namespace="test-orchestrator-iperf2",
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

    assert job["spec"]["activeDeadlineSeconds"] == deadline_seconds(definition)
    assert job["spec"]["activeDeadlineSeconds"] > int(definition.duration.seconds)


def test_every_object_carries_the_ownership_labels(manifests: list[dict[str, Any]]) -> None:
    for manifest in manifests:
        labels = manifest["metadata"]["labels"]
        assert labels[MANAGED_BY_LABEL] == "test-orchestrator"
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


def test_the_definition_travels_with_the_run(manifests: list[dict[str, Any]]) -> None:
    # The Job reads the document from a ConfigMap, not from the submitter's disk, which
    # is what lets a reattaching process on another machine need nothing local.
    definition_map = manifests[1]
    assert definition_map["data"] == {"definition.yaml": "apiVersion: test-orchestrator/v1alpha1\n"}

    assert _container(manifests)["args"] == [
        "execute",
        "--definition",
        f"{DEFINITION_MOUNT_PATH}/definition.yaml",
        "--run-id",
        RUN_ID,
    ]


def test_a_robot_suite_stays_a_robot_suite(definition: TestDefinition) -> None:
    # The in-cluster parse picks its format from the suffix exactly as the local one does.
    manifests = render_manifests(
        definition,
        source="*** Settings ***\n",
        source_suffix=".robot",
        run_id=RUN_ID,
        image=IMAGE,
        namespace="test-orchestrator-iperf2",
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


def test_the_namespace_is_not_run_owned() -> None:
    # It is shared by every run of the test type, so a run-scoped cleanup selecting on
    # run-id must never match it.
    manifest = namespace_manifest("test-orchestrator-iperf2")

    assert manifest["metadata"]["labels"] == {MANAGED_BY_LABEL: "test-orchestrator"}
    assert RUN_ID_LABEL not in manifest["metadata"]["labels"]


def test_a_service_account_is_only_set_when_asked_for(definition: TestDefinition) -> None:
    without = render_manifests(
        definition,
        source="",
        source_suffix=".yaml",
        run_id=RUN_ID,
        image=IMAGE,
        namespace="ns",
    )
    with_account = render_manifests(
        definition,
        source="",
        source_suffix=".yaml",
        run_id=RUN_ID,
        image=IMAGE,
        namespace="ns",
        service_account="runner",
    )

    assert "serviceAccountName" not in _job(without)["spec"]["template"]["spec"]
    assert _job(with_account)["spec"]["template"]["spec"]["serviceAccountName"] == "runner"


def test_resource_names_use_the_run_id_verbatim() -> None:
    # ADR-02-08's format is already a valid object-name segment, so an object name and a
    # label spell the same run the same way. The previous format needed lowercasing here.
    assert resource_name(RUN_ID) == f"run-{RUN_ID}"
