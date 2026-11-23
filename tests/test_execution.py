"""Phases 5, 6 and 9a, and what ``Run Network Test`` does, without Robot Framework.

M1-11's third acceptance criterion is that the library's own tests pass with Robot
uninstalled. Everything here does: the runner is faked, and ``run_network_test`` runs
against an in-memory API server whose pods become Ready the moment they are created.
What Robot itself does with the suite is in ``test_robot_execution.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from dokimi.error_categories import ErrorCategory
from dokimi.execution import (
    PairExecution,
    PairMismatchError,
    RobotRunError,
    RunArtifacts,
    RunContext,
    SubprocessRobotRunner,
    ToolFailedError,
    default_robot_directory,
    execute_pairs,
    merge_reports,
    render_suite,
    run_network_test,
)
from dokimi.formats import parse_file, render_robot
from dokimi.images import ImageSource
from dokimi.k8s.deadlines import workload_budget
from dokimi.k8s.identity import RUN_ID_LABEL
from dokimi.k8s.ownership import owner_manifest
from dokimi.k8s.resources import CONFIG_MAP, POD, Kind, Manifest
from dokimi.model import ConfigError
from dokimi.planning import NodeFacts, Plan, PlannedPair
from dokimi.run_identity import RunId
from dokimi.testtypes.interface import Role, WorkloadImage
from dokimi.testtypes.iperf2 import CONTAINER_NAME, Iperf2, UnexpectedTargetError
from dokimi.testtypes.lifecycle import (
    CompletionTimeoutError,
    ReadinessGateError,
    WorkloadLostError,
)
from tests.fake_exporter import pair_serves
from tests.fake_resources import FakeResources
from tests.fake_robot import FakeRobotRunner

RUN_ID = "20261002-120000-a1b2c3"
NAMESPACE = "dokimi-iperf2"
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "worker-01-to-worker-02-udp.yaml"
IMAGES = {
    "orchestrator": WorkloadImage("orchestrator:dev", ImageSource.RUN_OVERRIDE),
    "iperf2": WorkloadImage("iperf2:dev", ImageSource.RUN_OVERRIDE),
}
SERVER_IP, CLIENT_IP = "10.244.1.5", "10.244.2.7"


# -- the run context --------------------------------------------------------------------


def test_a_robot_process_reads_back_the_context_it_was_started_with() -> None:
    context = RunContext(
        RUN_ID, NAMESPACE, "pair-001", "worker-01", "worker-02", "test-cluster", IMAGES
    )

    assert RunContext.from_environment(context.environment()) == context


def test_a_robot_process_started_by_hand_says_what_is_missing() -> None:
    with pytest.raises(
        ConfigError,
        match="DOKIMI_NAMESPACE, DOKIMI_TEST_ID, DOKIMI_SOURCE_NODE, DOKIMI_TARGET_NODE not set",
    ):
        RunContext.from_environment({"DOKIMI_RUN_ID": RUN_ID})


# -- phases 5, 6 and 9a -----------------------------------------------------------------


@pytest.fixture
def artifacts(tmp_path: Path) -> RunArtifacts:
    return RunArtifacts(tmp_path, RUN_ID)


def plan_of(*test_ids: str) -> Plan:
    source = NodeFacts(name="worker-01", cordoned=False, ready=True)
    target = NodeFacts(name="worker-02", cordoned=False, ready=True)
    return Plan(waves=tuple((PlannedPair(test_id, source, target),) for test_id in test_ids))


def test_phase_5_keeps_exactly_what_render_robot_renders(artifacts: RunArtifacts) -> None:
    definition = parse_file(EXAMPLE)

    suite = render_suite(definition, artifacts)

    assert suite == artifacts.directory / "suite.robot"
    assert suite.read_text(encoding="utf-8") == render_robot(definition)


def test_phase_6_runs_one_robot_process_per_pair(artifacts: RunArtifacts) -> None:
    runner = FakeRobotRunner()
    suite = render_suite(parse_file(EXAMPLE), artifacts)

    executions = execute_pairs(
        plan_of("pair-001", "pair-002"),
        suite=suite,
        artifacts=artifacts,
        runner=runner,
        robot_directory=Path("/opt/dokimi/robot"),
        namespace=NAMESPACE,
        cluster="test-cluster",
        images=IMAGES,
    )

    assert [run.environment["DOKIMI_TEST_ID"] for run in runner.runs] == ["pair-001", "pair-002"]
    assert {run.suite for run in runner.runs} == {suite}
    assert {run.pythonpath for run in runner.runs} == {Path("/opt/dokimi/robot")}
    assert [execution.output for execution in executions] == [
        artifacts.pair_output("pair-001"),
        artifacts.pair_output("pair-002"),
    ]


def test_a_pair_whose_process_wrote_nothing_stops_the_run(artifacts: RunArtifacts) -> None:
    runner = FakeRobotRunner(writes_output=False)

    with pytest.raises(RobotRunError, match="pair-001: Robot Framework wrote no pair-001.xml"):
        execute_pairs(
            plan_of("pair-001"),
            suite=render_suite(parse_file(EXAMPLE), artifacts),
            artifacts=artifacts,
            runner=runner,
            robot_directory=Path("robot"),
            namespace=NAMESPACE,
            cluster="test-cluster",
            images=IMAGES,
        )


def test_phase_9a_merges_every_pair_even_when_there_is_one(artifacts: RunArtifacts) -> None:
    runner = FakeRobotRunner()
    artifacts.create()
    pair = plan_of("pair-001").pairs[0]

    merge_reports([PairExecution(pair, artifacts.pair_output("pair-001"))], artifacts, runner)

    assert runner.merges == [[artifacts.pair_output("pair-001")]]
    assert artifacts.log.is_file()
    assert artifacts.report.is_file()


# -- the subprocess runner --------------------------------------------------------------


class Recorded:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, command: list[str], *, env: dict[str, str], check: bool) -> Any:
        self.calls.append((command, env))
        return subprocess.CompletedProcess(command, self.code)


def test_the_runner_writes_only_output_xml_per_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded = Recorded()
    monkeypatch.setattr(subprocess, "run", recorded)

    SubprocessRobotRunner("python").run(
        tmp_path / "suite.robot",
        output=tmp_path / "pair.xml",
        environment={"DOKIMI_TEST_ID": "pair-001"},
        pythonpath=tmp_path / "robot",
    )

    command, env = recorded.calls[0]
    assert command[:3] == ["python", "-m", "robot"]
    assert command[command.index("--log") + 1] == "NONE"
    assert command[command.index("--report") + 1] == "NONE"
    assert command[-1] == str(tmp_path / "suite.robot")
    assert env["DOKIMI_TEST_ID"] == "pair-001"


@pytest.mark.parametrize("code", [0, 1, 3, 250])
def test_failed_tests_are_not_the_runners_business(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
) -> None:
    # Robot's exit code counts failed tests. The results say which, so it is never read
    # here, and never propagated as the run's.
    monkeypatch.setattr(subprocess, "run", Recorded(code))

    SubprocessRobotRunner().merge(
        [tmp_path / "a.xml"],
        output=tmp_path / "o.xml",
        log=tmp_path / "l.html",
        report=tmp_path / "r.html",
    )


@pytest.mark.parametrize(("code", "meaning"), [(252, "invalid data"), (255, "internal error")])
def test_a_robot_that_could_not_run_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int, meaning: str
) -> None:
    monkeypatch.setattr(subprocess, "run", Recorded(code))

    with pytest.raises(RobotRunError, match=meaning):
        SubprocessRobotRunner().run(
            tmp_path / "s.robot", output=tmp_path / "o.xml", environment={}, pythonpath=tmp_path
        )


def test_the_robot_directory_is_configurable_and_defaults_to_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOKIMI_ROBOT_DIR", raising=False)
    assert (default_robot_directory() / "resources" / "Dokimi.resource").is_file()

    monkeypatch.setenv("DOKIMI_ROBOT_DIR", "/opt/dokimi/robot")
    assert default_robot_directory() == Path("/opt/dokimi/robot")


def test_a_robot_killed_by_a_signal_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", Recorded(-9))

    with pytest.raises(RobotRunError, match="killed by signal 9"):
        SubprocessRobotRunner().run(
            tmp_path / "s.robot", output=tmp_path / "o.xml", environment={}, pythonpath=tmp_path
        )


def test_an_output_left_from_an_earlier_attempt_is_never_read_as_this_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = tmp_path / "o.xml"
    stale.write_text("<robot/>")
    monkeypatch.setattr(subprocess, "run", Recorded())

    SubprocessRobotRunner().run(
        tmp_path / "s.robot", output=stale, environment={}, pythonpath=tmp_path
    )

    assert not stale.exists()


# -- Run Network Test -------------------------------------------------------------------


class Kubelet(FakeResources):
    """An API server whose pods start at once, each with an IP, unless told otherwise."""

    def __init__(self, waiting_reason: str | None = None) -> None:
        super().__init__()
        self.waiting_reason = waiting_reason

    def set_status(self, namespace: str, name: str, status: dict[str, Any]) -> None:
        """What a kubelet does: change a pod's status, out of band of its spec."""
        with self._lock:
            self.objects[(POD.kind, namespace, name)]["status"] = status

    def create(self, kind: Kind, namespace: str, body: Manifest) -> Manifest:
        created = super().create(kind, namespace, body)
        if kind is POD:
            role = created["metadata"]["labels"]["app.kubernetes.io/component"]
            ip = SERVER_IP if role == Role.SERVER.value else CLIENT_IP
            status: dict[str, Any] = {
                "phase": "Running",
                "podIP": ip,
                "podIPs": [{"ip": ip}],
                "conditions": [{"type": "Ready", "status": "True"}],
            }
            if self.waiting_reason:
                status = {
                    "phase": "Pending",
                    "containerStatuses": [
                        {
                            "name": CONTAINER_NAME,
                            "state": {"waiting": {"reason": self.waiting_reason}},
                        }
                    ],
                }
            self.set_status(namespace, created["metadata"]["name"], status)
        return created


def client_finishes(kubelet: Kubelet, *, exit_code: int = 0, destination: str = SERVER_IP) -> Any:
    """A trigger that plays the client: it runs, and its pod reports how it ended."""
    calls: list[str] = []

    def trigger(host: str, port: int, payload: bytes) -> None:
        calls.append(host)
        name = f"run-{RUN_ID}-pair-001-client"
        pod = kubelet.get(POD, NAMESPACE, name)
        summary = (
            f"20261002120010,{CLIENT_IP},5102,{destination},5001,1,0.0-5.0,312,499,0.01,0,21,0.0,0"
            if exit_code == 0
            else ""
        )
        status = dict(pod["status"])
        status["containerStatuses"] = [
            {
                "name": CONTAINER_NAME,
                "state": {"terminated": {"exitCode": exit_code, "message": summary, "reason": "x"}},
            }
        ]
        kubelet.set_status(NAMESPACE, name, status)

    trigger.calls = calls  # type: ignore[attr-defined]
    return trigger


def owned(kubelet: Kubelet) -> Kubelet:
    kubelet.put(CONFIG_MAP, NAMESPACE, owner_manifest(RunId(RUN_ID), NAMESPACE))
    return kubelet


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def run(
    kubelet: Kubelet,
    trigger: Any,
    *,
    context: RunContext | None = None,
    plugin: Any = None,
    read_metrics: Any = None,
    **overrides: str,
) -> Any:
    clock = Clock()
    arguments = {
        "test_type": "iperf2",
        "protocol": "udp",
        "source": "worker-01",
        "target": "worker-02",
        "bandwidth": "50M",
        "duration": "5s",
    } | overrides
    return run_network_test(
        context
        or RunContext(
            RUN_ID, NAMESPACE, "pair-001", "worker-01", "worker-02", "test-cluster", IMAGES
        ),
        cluster=kubelet,
        plugin=plugin or Iperf2(image=IMAGES["iperf2"], trigger=trigger),
        clock=clock,
        sleep=clock.sleep,
        read_metrics=read_metrics
        or pair_serves(
            run_id=RUN_ID,
            protocol=arguments["protocol"],
            server_ip=SERVER_IP,
            client_ip=CLIENT_IP,
        ),
        **arguments,
    )


def test_a_pair_is_deployed_started_and_waited_for() -> None:
    kubelet = owned(Kubelet())
    trigger = client_finishes(kubelet)

    result = run(kubelet, trigger)

    assert trigger.calls == [CLIENT_IP]
    assert result.pods == {
        Role.SERVER: f"run-{RUN_ID}-pair-001-server",
        Role.CLIENT: f"run-{RUN_ID}-pair-001-client",
    }
    assert result.run_metadata["workload_image_source"] == "run-override"
    assert result.summary.split(",")[3] == SERVER_IP


def test_its_pods_are_run_owned_and_bounded_by_the_pairs_budget() -> None:
    kubelet = owned(Kubelet())
    run(kubelet, client_finishes(kubelet))

    pods = kubelet.list(POD, f"{RUN_ID_LABEL}={RUN_ID}", NAMESPACE)
    expected = workload_budget(5.0, warmup_seconds=10.0).active_deadline_seconds
    assert len(pods) == 2
    assert {pod["spec"]["activeDeadlineSeconds"] for pod in pods} == {expected}


def test_a_pod_that_cannot_pull_its_image_never_starts_the_test() -> None:
    kubelet = owned(Kubelet(waiting_reason="ImagePullBackOff"))
    trigger = client_finishes(kubelet)

    with pytest.raises(ReadinessGateError) as raised:
        run(kubelet, trigger)

    assert raised.value.category is ErrorCategory.IMAGE_PULL
    assert trigger.calls == []


def test_a_tool_that_measured_nothing_fails_the_test() -> None:
    kubelet = owned(Kubelet())

    with pytest.raises(ToolFailedError, match="iperf2 exited 69 without a summary line"):
        run(kubelet, client_finishes(kubelet, exit_code=69))


def test_a_spoofed_trigger_shows_up_here_and_nowhere_else() -> None:
    # start() cannot tell (adrs#46); completion() can, and this is the path that uses it.
    kubelet = owned(Kubelet())

    with pytest.raises(UnexpectedTargetError, match="measured against 10.9.9.9"):
        run(kubelet, client_finishes(kubelet, destination="10.9.9.9"))


def test_a_parameter_the_test_type_refuses_creates_nothing() -> None:
    kubelet = owned(Kubelet())

    with pytest.raises(ConfigError):
        run(kubelet, client_finishes(kubelet), protocol="sctp")

    assert kubelet.list(POD, f"{RUN_ID_LABEL}={RUN_ID}", NAMESPACE) == []


def test_a_suite_cannot_run_another_pair_than_the_planned_one() -> None:
    kubelet = owned(Kubelet())

    with pytest.raises(PairMismatchError, match="runs the planned pair worker-01 -> worker-02"):
        run(kubelet, client_finishes(kubelet), target="worker-03")

    assert kubelet.list(POD, f"{RUN_ID_LABEL}={RUN_ID}", NAMESPACE) == []


def test_a_run_without_an_image_for_its_test_type_resolves_none_of_its_own() -> None:
    # submit resolves every image; a default chosen here nobody would have seen.
    kubelet = owned(Kubelet())
    context = RunContext(
        RUN_ID, NAMESPACE, "pair-001", "worker-01", "worker-02", "test-cluster", {}
    )

    with pytest.raises(ConfigError, match="this run has no image for iperf2"):
        run_network_test(
            context,
            test_type="iperf2",
            protocol="udp",
            source="worker-01",
            target="worker-02",
            bandwidth="50M",
            duration="5s",
            cluster=kubelet,
        )

    assert kubelet.list(POD, f"{RUN_ID_LABEL}={RUN_ID}", NAMESPACE) == []


class Silent:
    """A plugin that cannot say how its test finished: it has no ``completion()``."""


def test_a_plugin_that_cannot_report_completion_fails_closed() -> None:
    kubelet = owned(Kubelet())

    with pytest.raises(TypeError, match="cannot report how its test finished"):
        run(kubelet, client_finishes(kubelet), plugin=Silent())

    assert kubelet.list(POD, f"{RUN_ID_LABEL}={RUN_ID}", NAMESPACE) == []


def test_a_test_that_never_finishes_times_out_within_its_budget() -> None:
    kubelet = owned(Kubelet())

    with pytest.raises(CompletionTimeoutError):
        run(kubelet, lambda host, port, payload: None)


def test_a_client_pod_that_disappears_loses_the_test() -> None:
    kubelet = owned(Kubelet())

    def trigger(host: str, port: int, payload: bytes) -> None:
        name = f"run-{RUN_ID}-pair-001-client"
        pod = kubelet.get(POD, NAMESPACE, name)
        kubelet.delete(POD, NAMESPACE, name, uid=pod["metadata"]["uid"])

    with pytest.raises(WorkloadLostError, match="disappeared before it finished"):
        run(kubelet, trigger)
