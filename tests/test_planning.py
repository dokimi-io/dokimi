"""Phase 3: discovery, explicit node resolution and the plan.

Planning is a pure function of a definition and a node snapshot, so it is tested as one:
build the snapshot, plan against it, look at what comes back. Nothing here touches a
cluster, fake or otherwise, except the one test that proves discovery reads once.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from dokimi.model import (
    Bandwidth,
    Comparison,
    ConfigError,
    Criterion,
    Duration,
    Metric,
    Protocol,
    TestDefinition,
    TestType,
    Unit,
)
from dokimi.planning import (
    Concurrency,
    NodeInventory,
    Plan,
    discover,
    plan_run,
)
from dokimi.planning.nodes import node_facts
from tests.fake_cluster import FakeCluster, node

CONTROL_PLANE_TAINT = ("node-role.kubernetes.io/control-plane", "", "NoSchedule")


@pytest.fixture
def definition() -> TestDefinition:
    return TestDefinition(
        name="pair",
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


def inventory(*nodes: dict[str, object]) -> NodeInventory:
    return NodeInventory.of(node_facts(n) for n in nodes)


# -- the acceptance criteria --------------------------------------------------


def test_two_valid_node_names_produce_one_directed_pair(definition: TestDefinition) -> None:
    plan = plan_run(definition, inventory(node("worker-01"), node("worker-02")))

    assert len(plan.pairs) == 1
    pair = plan.pairs[0]
    assert (pair.source.name, pair.target.name) == ("worker-01", "worker-02")


def test_direction_is_preserved(definition: TestDefinition) -> None:
    # A->B and B->A are different tests, because asymmetric paths are exactly
    # what the tool exists to find. Planning must not normalise the order.
    reversed_definition = replace(definition, source_node="worker-02", target_node="worker-01")

    plan = plan_run(reversed_definition, inventory(node("worker-01"), node("worker-02")))

    assert (plan.pairs[0].source.name, plan.pairs[0].target.name) == ("worker-02", "worker-01")


def test_a_missing_node_is_a_config_error(definition: TestDefinition) -> None:
    with pytest.raises(ConfigError, match="target node 'worker-02' does not exist"):
        plan_run(definition, inventory(node("worker-01"), node("worker-03")))


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (node("worker-02", cordoned=True), "cordoned"),
        (node("worker-02", ready="False"), "not Ready"),
        (node("worker-02", ready="Unknown"), "not Ready"),
        (node("worker-02", ready=None), "not Ready"),
        (node("worker-02", taints=[CONTROL_PLANE_TAINT]), "control-plane:NoSchedule"),
        (node("worker-02", taints=[("dedicated", "gpu", "NoExecute")]), "dedicated=gpu:NoExecute"),
    ],
)
def test_an_unschedulable_node_is_a_config_error(
    definition: TestDefinition, target: dict[str, object], reason: str
) -> None:
    with pytest.raises(ConfigError, match="cannot accept a test pod") as error:
        plan_run(definition, inventory(node("worker-01"), target))

    assert reason in str(error.value)


# -- schedulability -----------------------------------------------------------


def test_a_preference_is_not_a_prohibition(definition: TestDefinition) -> None:
    # PreferNoSchedule asks the scheduler to avoid the node; it does not forbid it.
    soft = node("worker-02", taints=[("maintenance", "soon", "PreferNoSchedule")])

    plan = plan_run(definition, inventory(node("worker-01"), soft))

    assert plan.pairs[0].target.name == "worker-02"


def test_every_reason_is_reported_not_just_the_first(definition: TestDefinition) -> None:
    # Someone fixing one problem should not have to re-run to discover the next.
    broken = node("worker-02", cordoned=True, ready="False", taints=[CONTROL_PLANE_TAINT])

    with pytest.raises(ConfigError) as error:
        plan_run(definition, inventory(node("worker-01"), broken))

    message = str(error.value)
    assert "cordoned" in message
    assert "not Ready" in message
    assert "NoSchedule" in message


def test_a_cordon_is_reported_once_not_twice(definition: TestDefinition) -> None:
    # Cordoning a node makes Kubernetes add its own `unschedulable` taint. Found on a real
    # cluster: reporting both reads as two problems when there is one.
    cordoned = node(
        "worker-02",
        cordoned=True,
        taints=[("node.kubernetes.io/unschedulable", "", "NoSchedule")],
    )

    with pytest.raises(ConfigError) as error:
        plan_run(definition, inventory(node("worker-01"), cordoned))

    message = str(error.value)
    assert "cordoned" in message
    assert "node.kubernetes.io/unschedulable" not in message


def test_the_source_is_checked_as_well_as_the_target(definition: TestDefinition) -> None:
    with pytest.raises(ConfigError, match="source node 'worker-01'"):
        plan_run(definition, inventory(node("worker-01", cordoned=True), node("worker-02")))


# -- error messages -----------------------------------------------------------


def test_a_near_miss_gets_a_suggestion(definition: TestDefinition) -> None:
    typo = replace(definition, target_node="worker-2")

    with pytest.raises(ConfigError, match="did you mean 'worker-02'"):
        plan_run(typo, inventory(node("worker-01"), node("worker-02")))


def test_the_available_nodes_are_listed(definition: TestDefinition) -> None:
    with pytest.raises(ConfigError, match="The cluster's nodes are: alpha, worker-01"):
        plan_run(definition, inventory(node("worker-01"), node("alpha")))


def test_a_large_cluster_is_summarised_not_dumped(definition: TestDefinition) -> None:
    many = [node(f"node-{i:03d}") for i in range(40)]

    with pytest.raises(ConfigError) as error:
        plan_run(definition, inventory(*many))

    assert "and 30 more" in str(error.value)


def test_an_empty_cluster_says_so(definition: TestDefinition) -> None:
    with pytest.raises(ConfigError, match="reports no nodes at all"):
        plan_run(definition, inventory())


# -- the plan's shape ---------------------------------------------------------


def test_milestone_01_plans_sequentially(definition: TestDefinition) -> None:
    plan = plan_run(definition, inventory(node("worker-01"), node("worker-02")))

    assert plan.concurrency is Concurrency.SEQUENTIAL
    assert plan.max_concurrency == 1
    assert all(len(wave) == 1 for wave in plan.waves)


def test_an_empty_plan_is_refused() -> None:
    # A selection yielding zero tests is an error, not a run that passes
    # having measured nothing.
    with pytest.raises(ConfigError, match="measure nothing"):
        Plan(waves=())


def test_test_ids_are_label_safe(definition: TestDefinition) -> None:
    long_names = replace(
        definition, source_node="a" * 200 + "-source", target_node="a" * 200 + "-target"
    )
    plan = plan_run(long_names, inventory(node("a" * 200 + "-source"), node("a" * 200 + "-target")))

    # A node name may be up to 253 characters; a label value may not exceed 63.
    assert len(plan.pairs[0].test_id) <= 63


def test_planning_is_deterministic(definition: TestDefinition) -> None:
    # The matrix is a pure function of the selection and the mode, whatever
    # order the API happened to return nodes in.
    forwards = inventory(node("worker-01"), node("worker-02"), node("worker-03"))
    backwards = inventory(node("worker-03"), node("worker-02"), node("worker-01"))

    assert plan_run(definition, forwards) == plan_run(definition, backwards)
    assert forwards.names == ("worker-01", "worker-02", "worker-03")


# -- run metadata -------------------------------------------------------------


def test_the_plan_is_recorded_in_the_run_metadata(definition: TestDefinition) -> None:
    plan = plan_run(definition, inventory(node("worker-01"), node("worker-02")))

    assert plan.run_metadata() == {
        "placement_mode": "explicit",
        "concurrency": "sequential",
        "max_concurrency": 1,
        "planned_tests": 1,
        "pairs": [
            {
                "test_id": "pair-001",
                "wave": 0,
                "source_node": "worker-01",
                "target_node": "worker-02",
            }
        ],
    }


# -- discovery ----------------------------------------------------------------


def test_discovery_reads_the_cluster_once() -> None:
    # Selection is resolved once and the matrix does not change if the
    # cluster does. One read is what makes that true rather than aspirational.
    cluster = FakeCluster()

    snapshot = discover(cluster)

    assert cluster.node_reads == 1
    assert snapshot.names == ("worker-01", "worker-02")


def test_discovery_tolerates_sparse_node_objects() -> None:
    # A node the API returns with no spec, no taints and no conditions must still parse.
    facts = node_facts({"metadata": {"name": "bare"}})

    assert facts.name == "bare"
    assert facts.taints == ()
    assert not facts.ready
