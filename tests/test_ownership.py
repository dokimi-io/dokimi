"""Ownership labels, the placeholder owner, and deletion that only ever touches our objects.

Two of M1-05's acceptance criteria live here: an object without ownership labels survives
a cleanup of its namespace, and every object a run creates has a valid ownerReference.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import dokimi.k8s.ownership as ownership_module
from dokimi.k8s.deadlines import DeadlineBudget, bound_job, bound_pod
from dokimi.k8s.ownership import (
    MANAGED_BY_LABEL,
    ORCHESTRATOR_COMPONENT,
    OWNER_COMPONENT,
    RUN_ID_LABEL,
    TEST_ID_LABEL,
    OwnershipError,
    RunOwnership,
    delete_objects,
    delete_run_owned,
    find_run_owned,
    owner_name,
    ownership_labels,
)
from dokimi.k8s.resources import CONFIG_MAP, JOB, POD, SERVICE, ClusterError, Manifest
from dokimi.run_identity import RunId
from tests.fake_resources import FakeResources

RUN = RunId("20260924-143012-k3f9x2")
OTHER_RUN = RunId("20260924-120000-a1b2c3")
NAMESPACE = "dokimi-iperf2"
BUDGET = DeadlineBudget(
    test_duration=10, startup=60, warmup=0, direct_read=5, scrape_confirmation=30
)


def pod(name: str, namespace: str = NAMESPACE, **metadata: Any) -> Manifest:
    return bound_pod(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, **metadata},
            "spec": {"containers": [{"name": "iperf2", "image": "iperf2"}]},
        },
        BUDGET,
    )


def service(name: str, namespace: str = NAMESPACE) -> Manifest:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"ports": [{"port": 5001}]},
    }


@pytest.fixture
def cluster() -> FakeResources:
    return FakeResources()


@pytest.fixture
def ownership(cluster: FakeResources) -> RunOwnership:
    return RunOwnership(cluster, RUN, metadata={"orchestrator_image": "ghcr.io/x@sha256:ab"})


# -- labels and the owner object ---------------------------------------------------------


def test_created_objects_carry_the_ownership_labels(
    cluster: FakeResources, ownership: RunOwnership
) -> None:
    created = ownership.create(POD, pod("iperf2-server"), test_id="t-001")

    labels = created["metadata"]["labels"]
    assert labels[MANAGED_BY_LABEL] == "dokimi"
    assert labels[RUN_ID_LABEL] == str(RUN)
    assert labels[TEST_ID_LABEL] == "t-001"


def test_the_owner_is_created_before_anything_else_in_its_namespace(
    cluster: FakeResources, ownership: RunOwnership
) -> None:
    ownership.create(POD, pod("iperf2-server"))

    first_kind, first = cluster.created[0]
    assert first_kind == CONFIG_MAP
    assert first["metadata"]["name"] == owner_name(RUN)


def test_owner_shape(cluster: FakeResources, ownership: RunOwnership) -> None:
    owner = ownership.owner_in(NAMESPACE)

    assert owner["metadata"]["name"] == f"run-{RUN}-owner"
    assert owner["metadata"]["labels"] == ownership_labels(RUN, component=OWNER_COMPONENT)
    assert owner["data"]["run_id"] == str(RUN)
    assert datetime.fromisoformat(owner["data"]["created_at"]).tzinfo == UTC
    assert owner["data"]["orchestrator_image"] == "ghcr.io/x@sha256:ab"
    assert "ownerReferences" not in owner["metadata"]


def test_every_created_object_has_a_valid_owner_reference(
    cluster: FakeResources, ownership: RunOwnership
) -> None:
    cluster.namespaces.add("dokimi-prometheus")
    ownership.create(POD, pod("iperf2-server"), test_id="t-001")
    ownership.create(POD, pod("iperf2-client"), test_id="t-001")
    ownership.create(SERVICE, service("iperf2-server"), test_id="t-001")
    ownership.create(SERVICE, service("prometheus", "dokimi-prometheus"))
    ownership.create(
        JOB,
        bound_job(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "probe", "namespace": NAMESPACE},
                "spec": {"template": {"spec": {"containers": []}}},
            },
            BUDGET,
        ),
    )

    dependents = [
        obj for kind, obj in cluster.created if obj["metadata"]["name"] != owner_name(RUN)
    ]
    assert len(dependents) == 5
    for obj in dependents:
        namespace = obj["metadata"]["namespace"]
        (reference,) = obj["metadata"]["ownerReferences"]
        owner = cluster.get(CONFIG_MAP, namespace, reference["name"])
        # Valid: same namespace (GC does not cross them), same uid, and really ours.
        assert reference["uid"] == owner["metadata"]["uid"]
        assert reference["kind"] == "ConfigMap"
        assert owner["metadata"]["labels"][RUN_ID_LABEL] == str(RUN)


def test_one_owner_per_namespace(cluster: FakeResources, ownership: RunOwnership) -> None:
    cluster.namespaces.add("dokimi-prometheus")
    ownership.create(POD, pod("a"))
    ownership.create(POD, pod("b"))
    ownership.create(SERVICE, service("c", "dokimi-prometheus"))

    owners = [obj for kind, obj in cluster.created if kind == CONFIG_MAP]
    assert sorted(o["metadata"]["namespace"] for o in owners) == [
        "dokimi-iperf2",
        "dokimi-prometheus",
    ]


def test_an_existing_owner_of_this_run_is_adopted(cluster: FakeResources) -> None:
    first = RunOwnership(cluster, RUN).owner_in(NAMESPACE)
    again = RunOwnership(cluster, RUN).owner_in(NAMESPACE)

    assert again["metadata"]["uid"] == first["metadata"]["uid"]


def test_a_same_named_object_that_is_not_ours_is_not_adopted(cluster: FakeResources) -> None:
    cluster.put(CONFIG_MAP, NAMESPACE, {"metadata": {"name": owner_name(RUN)}, "data": {}})

    with pytest.raises(OwnershipError, match="refusing to adopt"):
        RunOwnership(cluster, RUN).owner_in(NAMESPACE)


def test_workloads_without_a_deadline_are_refused(ownership: RunOwnership) -> None:
    unbounded = pod("iperf2-client")
    del unbounded["spec"]["activeDeadlineSeconds"]

    with pytest.raises(OwnershipError, match="activeDeadlineSeconds"):
        ownership.create(POD, unbounded)


def test_objects_without_an_explicit_namespace_are_refused(ownership: RunOwnership) -> None:
    manifest = pod("iperf2-server")
    del manifest["metadata"]["namespace"]

    with pytest.raises(OwnershipError, match="namespace"):
        ownership.create(POD, manifest)


def test_conflicting_ownership_labels_are_refused(ownership: RunOwnership) -> None:
    manifest = pod("iperf2-server", labels={RUN_ID_LABEL: str(OTHER_RUN)})

    with pytest.raises(OwnershipError, match="conflicts"):
        ownership.create(POD, manifest)


def test_other_labels_are_kept(ownership: RunOwnership) -> None:
    created = ownership.create(POD, pod("iperf2-server", labels={"role": "server"}))

    assert created["metadata"]["labels"]["role"] == "server"


def test_stamp_does_not_mutate_its_input(ownership: RunOwnership) -> None:
    manifest = pod("iperf2-server")
    ownership.stamp(manifest)

    assert "labels" not in manifest["metadata"]
    assert "ownerReferences" not in manifest["metadata"]


# -- scoped deletion ---------------------------------------------------------------------


def test_unlabelled_objects_survive_a_cleanup_of_their_namespace(
    cluster: FakeResources, ownership: RunOwnership
) -> None:
    ownership.create(POD, pod("iperf2-server"))
    # Someone else's objects in the very same namespace, including a name we might use.
    cluster.put(POD, NAMESPACE, pod("unrelated"))
    cluster.put(SERVICE, NAMESPACE, service("iperf2-client"))
    cluster.put(
        POD, NAMESPACE, pod("half-labelled", labels={RUN_ID_LABEL: str(RUN)})
    )  # run-id without managed-by is not ours either

    report = delete_run_owned(cluster, RUN, namespace=NAMESPACE)

    assert report.complete
    assert not cluster.exists(POD, NAMESPACE, "iperf2-server")
    assert not cluster.exists(CONFIG_MAP, NAMESPACE, owner_name(RUN))
    assert cluster.exists(POD, NAMESPACE, "unrelated")
    assert cluster.exists(SERVICE, NAMESPACE, "iperf2-client")
    assert cluster.exists(POD, NAMESPACE, "half-labelled")


def test_cleanup_leaves_other_runs_alone(cluster: FakeResources) -> None:
    RunOwnership(cluster, RUN).create(POD, pod("mine"))
    RunOwnership(cluster, OTHER_RUN).create(POD, pod("theirs"))

    delete_run_owned(cluster, RUN)

    assert not cluster.exists(POD, NAMESPACE, "mine")
    assert cluster.exists(POD, NAMESPACE, "theirs")


def test_cleanup_never_deletes_the_orchestrator_itself(cluster: FakeResources) -> None:
    ownership = RunOwnership(cluster, RUN)
    ownership.create(POD, pod("orchestrator"), component=ORCHESTRATOR_COMPONENT)

    delete_run_owned(cluster, RUN)

    assert cluster.exists(POD, NAMESPACE, "orchestrator")


def test_cleanup_can_be_scoped_to_components(cluster: FakeResources) -> None:
    ownership = RunOwnership(cluster, RUN)
    ownership.create(POD, pod("client"), component="client")
    ownership.create(POD, pod("exporter"), component="node-exporter")

    report = delete_run_owned(cluster, RUN, kinds=(POD,), components={"client", "server"})

    assert [obj.name for obj in report.deleted] == ["client"]
    assert cluster.exists(POD, NAMESPACE, "exporter")


def test_dependents_are_deleted_before_owners(
    cluster: FakeResources, ownership: RunOwnership
) -> None:
    ownership.create(POD, pod("iperf2-server"))

    report = delete_run_owned(cluster, RUN)

    assert [obj.is_owner for obj in report.deleted] == [False, True]


def test_a_chosen_set_of_objects_is_deleted_only_if_it_is_the_runs(
    cluster: FakeResources, ownership: RunOwnership
) -> None:
    # A caller that picks which of a run's objects go, such as some tests' pods, can't
    # hand in another run's by mistake.
    ownership.create(POD, pod("ours"), test_id="pair-002")
    cluster.put(POD, NAMESPACE, pod("theirs", labels=ownership_labels(OTHER_RUN)))
    objects = find_run_owned(cluster, RUN, kinds=(POD,)) + find_run_owned(
        cluster, OTHER_RUN, kinds=(POD,)
    )

    report = delete_objects(cluster, RUN, objects)

    assert [(obj.name, obj.test_id) for obj in report.deleted] == [("ours", "pair-002")]
    assert cluster.exists(POD, NAMESPACE, "theirs")


def test_a_same_named_replacement_is_not_deleted(
    cluster: FakeResources, ownership: RunOwnership, monkeypatch: pytest.MonkeyPatch
) -> None:
    ownership.create(POD, pod("iperf2-server"))
    selected = find_run_owned(cluster, RUN)
    # Between selection and deletion, our pod goes away and an unrelated one takes its name.
    (ours,) = [obj for obj in selected if obj.kind == POD]
    cluster.delete(POD, NAMESPACE, "iperf2-server", uid=ours.uid)
    cluster.put(POD, NAMESPACE, pod("iperf2-server"))
    monkeypatch.setattr(ownership_module, "find_run_owned", lambda *a, **k: selected)

    report = delete_run_owned(cluster, RUN)

    assert cluster.exists(POD, NAMESPACE, "iperf2-server")
    assert ours in report.already_gone


def test_deletion_failures_are_reported_not_swallowed(
    cluster: FakeResources, ownership: RunOwnership, monkeypatch: pytest.MonkeyPatch
) -> None:
    ownership.create(POD, pod("iperf2-server"))

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise ClusterError("HTTP 403: Forbidden")

    monkeypatch.setattr(cluster, "delete", refuse)
    report = delete_run_owned(cluster, RUN)

    assert not report.complete
    assert {str(obj) for obj, _ in report.failed} == {
        f"Pod/iperf2-server in {NAMESPACE}",
        f"ConfigMap/{owner_name(RUN)} in {NAMESPACE}",
    }
