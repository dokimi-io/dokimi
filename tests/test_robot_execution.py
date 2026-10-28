"""What Robot Framework does with the rendered suite: phases 6 and 9a for real.

Skipped where Robot Framework is not installed, as in CI's core-only job: the library
must not need it (ADR-01-02), and ``test_execution.py`` covers everything that does not.

The cluster-facing step, ``run_network_test``, is replaced in-process here, because
that is the library's, tested in ``test_execution.py``. What is real is everything Robot
owns: parsing the rendered suite, resolving the resource and the library, calling the
keywords with their arguments, the report and the merge.
"""

from __future__ import annotations

import importlib.metadata
import io
import sys
from pathlib import Path
from typing import Any

import pytest

from dokimi import execution
from dokimi.execution import (
    NetworkTestResult,
    RunArtifacts,
    SubprocessRobotRunner,
    default_robot_directory,
    execute_pairs,
    merge_reports,
    render_suite,
)
from dokimi.formats import parse_file
from dokimi.formats.robot_suite import criterion_keyword_names
from dokimi.images import ImageSource
from dokimi.measurement import Measurement, MetricsMissingError, read_measurement
from dokimi.planning import NodeFacts, Plan, PlannedPair
from dokimi.testtypes.interface import Role, WorkloadImage
from dokimi.validation import read_validation


def _robot_installed() -> bool:
    # Not `import robot`: the repository's own robot/ directory makes that succeed as an
    # empty namespace package when Robot Framework is absent (robot/README.md).
    try:
        importlib.metadata.version("robotframework")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _robot_installed(), reason="needs Robot Framework")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
RUN_ID = "20261002-120000-a1b2c3"
IMAGES = {
    "orchestrator": WorkloadImage("orchestrator:dev", ImageSource.RUN_OVERRIDE),
    "iperf2": WorkloadImage("iperf2:dev", ImageSource.RUN_OVERRIDE),
}


@pytest.fixture
def run_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """What phase 6 starts each Robot process with."""
    context = execution.RunContext(
        RUN_ID, "dokimi-iperf2", "pair-001", "worker-01", "worker-02", "test-cluster", IMAGES
    )
    for name, value in context.environment().items():
        monkeypatch.setenv(name, value)


#: What the replaced ``run_network_test`` reports as read from the exporters. A test may
#: change it before running the suite; the fixture restores it.
MEASURED: dict[str, float] = {}


@pytest.fixture
def network_test(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """``run_network_test``, replaced: records each call, and the pair always completes."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sys.modules[__name__],
        "MEASURED",
        {"dokimi_throughput_bits_per_second": 49983531.0},
    )

    def completes(context: execution.RunContext, **arguments: str) -> NetworkTestResult:
        calls.append({"test_id": context.test_id, **arguments})
        return NetworkTestResult(
            test_id=context.test_id,
            pods={Role.SERVER: "server-pod", Role.CLIENT: "client-pod"},
            run_metadata={"address_family": "IPv4"},
            summary="20261002120010,10.0.0.3,5102,10.0.0.2,5001,1,0.0-5.0,1,1",
            measurement=Measurement(
                context.test_id,
                "inbound",
                dict(MEASURED),
                {
                    "run_id": context.run_id,
                    "test_id": context.test_id,
                    "direction": "inbound",
                    "test_type": "iperf2",
                    "protocol": arguments["protocol"],
                },
            ),
        )

    monkeypatch.setattr(execution, "run_network_test", completes)
    return calls


def run_in_process(suite: Path, directory: Path) -> Any:
    """Robot's own runner, in this process, so the replaced keyword step is the one used."""
    # By name, not `import robot`: to a type checker that is the repository's own robot/
    # directory, which has no `run` (robot/README.md explains the overlap).
    robot: Any = importlib.import_module("robot")
    from robot.api import ExecutionResult  # noqa: PLC0415

    robot.run(
        str(suite),
        output=str(directory / "output.xml"),
        log=str(directory / "log.html"),
        report=str(directory / "report.html"),
        pythonpath=[str(default_robot_directory())],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    return ExecutionResult(str(directory / "output.xml"))


def test_a_yaml_test_runs_through_its_rendered_suite_like_a_robot_one(
    tmp_path: Path, run_environment: None, network_test: list[dict[str, Any]]
) -> None:
    # M1-11's first acceptance criterion: authored in YAML or in Robot, the same suite
    # runs, the same keywords are called, the same report results.
    results = {}
    for name in ("worker-01-to-worker-02-udp.yaml", "worker-01-to-worker-02-udp.robot"):
        artifacts = RunArtifacts(tmp_path / name, RUN_ID)
        suite = render_suite(parse_file(EXAMPLES / name), artifacts)
        results[name] = (
            suite.read_text(encoding="utf-8"),
            run_in_process(suite, artifacts.directory),
        )
        assert artifacts.log.is_file()

    (yaml_suite, from_yaml), (robot_suite, from_robot) = results.values()
    assert yaml_suite == robot_suite
    yaml_test, robot_test = from_yaml.suite.tests[0], from_robot.suite.tests[0]
    assert (yaml_test.name, yaml_test.status, yaml_test.message) == (
        robot_test.name,
        robot_test.status,
        robot_test.message,
    )
    assert (
        network_test[0]
        == network_test[1]
        == {
            "test_id": "pair-001",
            "test_type": "iperf2",
            "protocol": "udp",
            "source": "worker-01",
            "target": "worker-02",
            "bandwidth": "1G",
            "duration": "30s",
        }
    )


def test_the_measurement_is_written_next_to_the_pairs_output(
    tmp_path: Path, run_environment: None, network_test: list[dict[str, Any]]
) -> None:
    # Phase 7: what the direct read found reaches the Job, which confirms Prometheus
    # holds the same values.
    artifacts = RunArtifacts(tmp_path, RUN_ID)
    suite = render_suite(parse_file(EXAMPLES / "worker-01-to-worker-02-udp.yaml"), artifacts)

    run_in_process(suite, artifacts.directory)

    measurement = read_measurement(artifacts.directory, "pair-001")
    assert measurement is not None
    assert measurement.direction == "inbound"
    assert measurement.values == {"dokimi_throughput_bits_per_second": 49983531.0}


def test_the_criteria_are_evaluated_against_what_the_pair_measured(
    tmp_path: Path, run_environment: None, network_test: list[dict[str, Any]]
) -> None:
    # Phase 8 for real: 49.98 Mbit/s against "at least 950" is a finding, and a packet
    # loss the exporters didn't serve is an error. Both are listed, apart, and the test
    # is tagged with the worse of the two.
    artifacts = RunArtifacts(tmp_path, RUN_ID)
    suite = render_suite(parse_file(EXAMPLES / "worker-01-to-worker-02-udp.yaml"), artifacts)

    test = run_in_process(suite, artifacts.directory).suite.tests[0]

    assert test.status == "FAIL"
    assert (
        "Throughput Should Be At Least 950 megabits per second: 1 of 1 pair(s) violated "
        "this criterion" in test.message
    )
    assert "49.98353 megabits per second, needs at least 950" in test.message
    assert (
        "ERROR (metrics_missing): Packet Loss Should Be At Most 0 percent: 1 of 1 pair(s) "
        "could not be evaluated" in test.message
    )
    assert "outcome:error" in test.tags
    assert "error:metrics_missing" in test.tags
    assert "outcome:fail" not in test.tags
    document = read_validation(artifacts.directory, "pair-001")
    assert document is not None
    assert document["outcome"] == "error"
    assert [c["outcome"] for c in document["criteria"]] == ["fail", "error"]


def test_a_pair_that_meets_its_criteria_passes(
    tmp_path: Path, run_environment: None, network_test: list[dict[str, Any]]
) -> None:
    MEASURED.update({"dokimi_throughput_bits_per_second": 9.6e8, "dokimi_packet_loss_percent": 0.0})
    artifacts = RunArtifacts(tmp_path, RUN_ID)
    suite = render_suite(parse_file(EXAMPLES / "worker-01-to-worker-02-udp.yaml"), artifacts)

    test = run_in_process(suite, artifacts.directory).suite.tests[0]

    assert test.status == "PASS"
    assert "outcome:pass" in test.tags
    document = read_validation(artifacts.directory, "pair-001")
    assert document is not None
    assert document["outcome"] == "pass"


def test_a_test_that_could_not_be_measured_is_tagged_with_the_error(
    tmp_path: Path, run_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(context: execution.RunContext, **arguments: str) -> NetworkTestResult:
        raise MetricsMissingError(context.test_id, "the server's exporter served no result")

    monkeypatch.setattr(execution, "run_network_test", fails)
    artifacts = RunArtifacts(tmp_path, RUN_ID)
    suite = render_suite(parse_file(EXAMPLES / "worker-01-to-worker-02-udp.yaml"), artifacts)

    test = run_in_process(suite, artifacts.directory).suite.tests[0]

    assert test.status == "FAIL"
    assert "metrics_missing: the server's exporter served no result" in test.message
    assert {"outcome:error", "error:metrics_missing"} <= set(test.tags)
    # Its criteria never ran, and it is still on record as an error, with the same
    # category as its tag: that is what the run's exit code is decided from.
    document = read_validation(artifacts.directory, "pair-001")
    assert document is not None
    assert document["outcome"] == "error"
    assert document["error"]["category"] == "metrics_missing"
    assert document["criteria"] == []


def test_an_error_nothing_anticipated_is_still_on_record_as_an_error(
    tmp_path: Path, run_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(context: execution.RunContext, **arguments: str) -> NetworkTestResult:
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(execution, "run_network_test", fails)
    artifacts = RunArtifacts(tmp_path, RUN_ID)
    suite = render_suite(parse_file(EXAMPLES / "worker-01-to-worker-02-udp.yaml"), artifacts)

    test = run_in_process(suite, artifacts.directory).suite.tests[0]

    assert "outcome:error" in test.tags
    assert not [tag for tag in test.tags if tag.startswith("error:")]
    document = read_validation(artifacts.directory, "pair-001")
    assert document is not None
    assert document["outcome"] == "error"
    assert document["error"] == {"category": None, "detail": "something nobody planned for"}


def test_robot_runs_as_its_own_process_and_rebot_merges_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real runner, with no cluster anywhere: the pair fails to reach one, and the
    # run still gets a merged log.html that says why. Nothing here may contact a real
    # cluster, so credentials are made unusable on purpose.
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "no-such-kubeconfig"))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)
    artifacts = RunArtifacts(tmp_path / "artifacts", RUN_ID)
    suite = render_suite(parse_file(EXAMPLES / "worker-01-to-worker-02-udp.yaml"), artifacts)
    source = NodeFacts(name="worker-01", cordoned=False, ready=True)
    target = NodeFacts(name="worker-02", cordoned=False, ready=True)
    runner = SubprocessRobotRunner()

    executions = execute_pairs(
        Plan(waves=((PlannedPair("pair-001", source, target),),)),
        suite=suite,
        artifacts=artifacts,
        runner=runner,
        robot_directory=default_robot_directory(),
        namespace="dokimi-iperf2",
        cluster="test-cluster",
        images=IMAGES,
    )
    merge_reports(executions, artifacts, runner)

    from robot.api import ExecutionResult  # noqa: PLC0415

    test = ExecutionResult(str(artifacts.output)).suite.tests[0]
    assert artifacts.log.is_file()
    assert artifacts.report.is_file()
    assert test.status == "FAIL"
    assert "no usable cluster credentials" in test.message


def test_the_library_implements_exactly_the_keywords_the_parser_reads() -> None:
    import sys  # noqa: PLC0415

    sys.path.insert(0, str(default_robot_directory() / "libraries"))
    try:
        from DokimiLibrary import DokimiLibrary  # type: ignore[import-not-found]  # noqa: PLC0415
    finally:
        sys.path.pop(0)

    names = DokimiLibrary().get_keyword_names()
    assert names[0] == "Run Network Test"
    assert names[1:] == list(criterion_keyword_names())
