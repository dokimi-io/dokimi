"""What each CLI command does, independent of how its arguments were parsed.

Phase 1 and nothing else. Everything here either runs entirely locally or talks to the
API server; no phase of a run happens on this side of the cluster boundary (ADR-01-06).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import yaml

from dokimi.artifact_stream import ArtifactReceiver, ArtifactStreamError, write_artifacts
from dokimi.cli.exit_codes import ExitCode
from dokimi.cluster_name import cluster_from_job_environment, resolve_cluster
from dokimi.diagnostics import SNAPSHOT_TIMEOUT_SECONDS, capture, write_report
from dokimi.execution import (
    ARTIFACTS_ROOT,
    PairExecution,
    RobotRunner,
    RunArtifacts,
    SubprocessRobotRunner,
    default_robot_directory,
    execute_pairs,
    merge_reports,
    render_suite,
)
from dokimi.formats import SUFFIXES, parse, parse_file, render_robot, render_yaml
from dokimi.k8s.cluster import ClusterClient, ClusterError, connect
from dokimi.k8s.exclusivity import claim_expiry, claim_run, mark_finished
from dokimi.k8s.identity import COMPONENT_LABEL, RUN_ID_LABEL, validate_run_id
from dokimi.k8s.manifests import job_deadline_seconds, resource_name
from dokimi.k8s.namespaces import namespace_for
from dokimi.k8s.ownership import (
    TEST_ID_LABEL,
    RunOwnership,
    delete_run_owned,
    find_run_owned,
    format_timestamp,
    owner_name,
    run_selector,
)
from dokimi.k8s.resources import CONFIG_MAP, POD, NotFoundError, ResourceClient
from dokimi.k8s.submission import Submission, follow, plan_submission, submit
from dokimi.measurement import Measurement, ScrapeReport, confirm_scrape, read_measurement
from dokimi.metrics.managed import (
    NODE_EXPORTER_COMPONENT,
    SCRAPE_INTERVAL_SECONDS,
    ManagedPrometheus,
    NodeExporter,
    detect_supporting_exporters,
    node_exporter_status,
    port_forward_command,
    provide_node_exporter,
    provision_managed_prometheus,
    removal_command,
    remove_node_exporter,
    select_monitoring,
)
from dokimi.metrics.prometheus import (
    DEFAULT_QUERY_ACCESS_TIMEOUT_SECONDS,
    PrometheusClient,
    verify_query_access,
)
from dokimi.model import DEFAULT_RESULTS_DIRECTORY, ConfigError, Criterion, TestDefinition, TestType
from dokimi.planning import Plan, PlannedPair, discover, plan_run
from dokimi.preflight import check_cluster, node_exporter_reachability
from dokimi.run_identity import RunId
from dokimi.run_images import (
    COMPONENTS,
    images_from_job_environment,
    parse_image_options,
    resolve_component,
    resolve_run_images,
)
from dokimi.testtypes.interface import Role, WorkloadImage
from dokimi.testtypes.probe import Probes

__all__ = [
    "Console",
    "attach",
    "convert",
    "execute",
    "preflight",
    "show_image",
    "submit_run",
    "validate",
]

#: How long phase 10 may take. Module-level so a test can shorten it.
DIAGNOSTICS_TIMEOUT_SECONDS = SNAPSHOT_TIMEOUT_SECONDS

#: How phase 6 runs Robot Framework. A module attribute so that a test can replace
#: the subprocess with a fake without a real Robot or a real cluster.
ROBOT_RUNNER: Callable[[], RobotRunner] = SubprocessRobotRunner

#: The plugin phase 4 probes the cluster with; ``None`` is the run's own. A module
#: attribute so that a test can replace its pods with ones that are already Ready.
PREFLIGHT_PLUGIN: Probes | None = None

#: How long phase 7 waits for Prometheus to hold a test's series: ``None`` is
#: ADR-03-01's three scrape intervals, at least 60 s. Module-level so a test can shorten it.
SCRAPE_CONFIRMATION_TIMEOUT_SECONDS: float | None = None

#: How long phase 4 waits for the run's Prometheus to answer a query. Module-level so a
#: test can shorten it without a real clock.
QUERY_ACCESS_TIMEOUT_SECONDS = DEFAULT_QUERY_ACCESS_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class Console:
    """Where a command writes. Injected so tests read output instead of capturing it."""

    out: TextIO
    err: TextIO

    def say(self, message: str = "") -> None:
        print(message, file=self.out)

    def warn(self, message: str) -> None:
        print(message, file=self.err)

    def fail(self, message: str) -> None:
        print(f"dokimi: {message}", file=self.err)

    def flush(self) -> None:
        self.out.flush()
        self.err.flush()


def convert(console: Console, *, path: Path, to: str) -> int:
    """Render a definition in the other format. Never touches a cluster.

    ``--to robot`` is ``render_robot()``, the same function phase 5 of every run calls,
    so this prints exactly the suite a run executes (ADR-01-08, ADR-04-05).
    """
    definition = parse_file(path)
    renderers = {"robot": render_robot, "yaml": render_yaml}
    console.out.write(renderers[to](definition))
    return ExitCode.PASS


def validate(console: Console, *, path: Path) -> int:
    """Parse a definition and report what it says. Never touches a cluster."""
    definition = parse_file(path)
    # A mode this build cannot run is refused here too, not first by the run.
    select_monitoring(definition.monitoring_mode)
    console.say(f"{path} is valid.")
    console.say()
    _describe(console, definition)
    return ExitCode.PASS


def submit_run(
    console: Console,
    *,
    path: Path,
    dry_run: bool = False,
    namespace: str | None = None,
    run_id: str | None = None,
    image_override: str | None = None,
    images: Sequence[str] = (),
    cluster_name: str | None = None,
    service_account: str | None = None,
    kubeconfig: str | None = None,
    context: str | None = None,
    follow_logs: bool = True,
    preflight_only: bool = False,
    connect_to: Callable[..., ClusterClient] | None = None,
) -> int:
    """Validate locally, then either render the run or create it.

    *preflight_only* submits the same run, whose Job stops after phase 4 (:func:`preflight`).

    Every image is resolved here, before anything else happens: the orchestrator's, and
    the ones the run deploys (ADR-04-08). A source build given no override for one of them
    stops with a config_error, rather than a Job that fails once it is running. So does a
    run without a cluster name, which has no default (ADR-02-07).
    """
    definition = parse_file(path)
    # Refused before anything is created, a dry run included: a run that could only
    # fail in phase 2 should never be submitted (ADR-05-05's config_error).
    monitoring = select_monitoring(definition.monitoring_mode)
    run_images = resolve_run_images(
        parse_image_options(images, orchestrator_image=image_override),
        monitoring=monitoring.deploys,
    )
    resolved_cluster = resolve_cluster(cluster_name)
    submission = plan_submission(
        definition,
        source=path.read_text(encoding="utf-8"),
        source_path=path,
        image=run_images["orchestrator"].reference,
        images=run_images,
        cluster=resolved_cluster,
        run_id=run_id,
        namespace=namespace,
        service_account=service_account,
        preflight_only=preflight_only,
    )

    if dry_run:
        # ADR-01-06: a dry run uses local discovery only, "or a provided kubeconfig for
        # read-only cluster info". Provided means provided: the ambient KUBECONFIG is not
        # consulted, so a plain dry run stays one that cannot contact anything.
        plan = None
        if kubeconfig is not None or context is not None:
            cluster = (connect_to or connect)(kubeconfig=kubeconfig, context=context)
            plan = plan_run(definition, discover(cluster))
        _render(console, definition, submission, plan)
        return ExitCode.PASS

    cluster = (connect_to or connect)(kubeconfig=kubeconfig, context=context)
    submit(cluster, submission)
    console.warn(f"submitted run {submission.run_id} to namespace {submission.namespace}")

    if not follow_logs:
        console.warn(f"not following. Reattach with: dokimi attach {submission.run_id}")
        return ExitCode.PASS

    return _follow(
        console,
        cluster,
        submission.namespace,
        submission.run_id,
        Path(definition.results_directory),
        expect_artifacts=not preflight_only,
    )


def preflight(
    console: Console,
    *,
    path: Path,
    namespace: str | None = None,
    run_id: str | None = None,
    image_override: str | None = None,
    images: Sequence[str] = (),
    service_account: str | None = None,
    cluster_name: str | None = None,
    kubeconfig: str | None = None,
    context: str | None = None,
    follow_logs: bool = True,
    connect_to: Callable[..., ClusterClient] | None = None,
) -> int:
    """Check the cluster for *path*'s run, and run no test: the standalone preflight.

    The same run as :func:`submit_run` submits, whose Job stops after phase 4. So it is
    the same code as a run's own preflight, not a second implementation of it (ADR-05-07):
    it claims the cluster, provisions the run's Prometheus, deploys the first pair in probe
    mode, and removes the pair again. It exits 0 if every check passed and 4 if one failed.

    Not a preview: ``--dry-run`` renders what a run would create and creates nothing,
    while this creates and schedules real pods, because that is the only way to see
    whether they schedule and connect.
    """
    return submit_run(
        console,
        path=path,
        namespace=namespace,
        run_id=run_id,
        image_override=image_override,
        images=images,
        service_account=service_account,
        cluster_name=cluster_name,
        kubeconfig=kubeconfig,
        context=context,
        follow_logs=follow_logs,
        preflight_only=True,
        connect_to=connect_to,
    )


def attach(
    console: Console,
    *,
    run_id: str,
    namespace: str | None = None,
    kubeconfig: str | None = None,
    context: str | None = None,
    results_directory: Path | None = None,
    connect_to: Callable[..., ClusterClient] | None = None,
) -> int:
    """Resume streaming a run this process never submitted.

    The artifacts land in *results_directory* if given, and otherwise in the
    ``results.directory`` of the definition the run was submitted with, which takes
    ``get`` on the run's ConfigMaps to read.
    """
    resolved_run_id = validate_run_id(run_id)
    # Milestone-01 runs one test type, so its namespace is derivable. Once a run can be
    # of another type, the namespace stops being guessable from nothing and --namespace
    # becomes the way to say which one.
    resolved_namespace = namespace_for(TestType.IPERF2.value, namespace)
    cluster = (connect_to or connect)(kubeconfig=kubeconfig, context=context)
    results = results_directory or _results_directory_of(
        console, cluster, resolved_namespace, resolved_run_id
    )
    return _follow(console, cluster, resolved_namespace, resolved_run_id, results)


def execute(
    console: Console,
    *,
    path: Path,
    run_id: str,
    namespace: str,
    deadline_seconds: int,
    connect_to: Callable[..., ClusterClient] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    env: Mapping[str, str] | None = None,
    artifacts_root: Path | None = None,
    robot_directory: Path | None = None,
    preflight_only: bool = False,
) -> int:
    """The in-cluster entry point: what the submitted Job runs.

    The CLI/Job boundary is one decision (ADR-01-06) and both of its sides belong to
    M1-04, so the contract is fixed here: the Job is invoked with the mounted definition
    and the run's id, and it owns phases 2 to 11.

    Phases 2 and 3 are implemented, and phase 4 begins with the run-exclusivity claim
    (ADR-05-10): a refused claim raises and exits 4 (ADR-01-11). Phase 4 then provisions
    the run's managed Prometheus, with a node-exporter when the cluster has none, and
    verifies it answers queries (ADR-03-02), before any workload exists. Then the cluster
    checks (ADR-05-07, :mod:`dokimi.preflight`): the planned nodes, and the first pair in
    probe mode, which is removed again before phase 5. A failed check exits 4 as well,
    and nothing in the matrix runs. Phases 5, 6 and 9a render the suite, run it through
    Robot Framework once per pair, and merge the reports into the run's artifact
    directory (ADR-01-08). Phase 10 takes the post-run snapshot on every path, before
    anything is removed, and writes ``diagnostics.json`` next to Robot's reports
    (ADR-05-07).

    With *preflight_only* the run stops after phase 4 and exits 0: ``dokimi preflight``.

    Phase 7's direct read happens in each pair's Robot process, which writes what it read
    next to its output; the Job then confirms Prometheus holds the same values, before
    anything is torn down (ADR-03-01).

    Not built yet: validation
    (M1-13, #48), the result artifact (M1-14, #49), and cleanup and the real exit code
    (M1-16, #51). Rather than exit 0 and let a submitter read that as a passing run, this
    reports an orchestrator error and names what is missing.

    Once claimed, the run is always marked finished on the way out — the start of phase
    11 (ADR-05-13) — so a run that ends, however it ends, stops holding the cluster.
    """
    resolved_run_id = RunId(validate_run_id(run_id))
    resolved_namespace = namespace_for(TestType.IPERF2.value, namespace)
    if deadline_seconds <= 0:
        raise ConfigError(f"--deadline-seconds must be positive, got {deadline_seconds}")

    console.say(f"run {run_id}")
    definition = parse_file(path)
    # Decided before any cluster call: a mode this build cannot honour is a config_error.
    monitoring = select_monitoring(definition.monitoring_mode)
    # Resolved by submit, never here: the submitter's environment does not reach the Job
    # (ADR-04-08). What the Job was given is what it runs, and the log says so.
    run_images = images_from_job_environment(env, monitoring=monitoring.deploys)
    run_cluster = cluster_from_job_environment(env)
    console.say("phase 2 (ingestion): definition parsed")
    console.say()
    _describe(console, definition)
    console.say(f"  cluster     {run_cluster}")
    _describe_images(console, run_images)
    console.say()

    # Phase 3. Discovery happens here, in the cluster, with the Job's own credentials:
    # this is the resolution the run is held to, and it happens before phase 6 deploys
    # anything, so a node that is missing or unschedulable is a config_error with
    # nothing to clean up (ADR-04-01, ADR-05-05).
    cluster = (connect_to or connect)()
    plan = plan_run(definition, discover(cluster))
    console.say(f"phase 3 (planning): {_plan_summary(plan)}")
    for line in _plan_lines(plan):
        console.say(line)
    console.say()

    # Phase 4 begins with the claim (ADR-05-10). The Job's deadline, its start time plus
    # activeDeadlineSeconds, is when the claim expires: by then the cluster has killed
    # this Job, so an expired claim is safe to ignore (ADR-05-13).
    resources = cluster.resources()
    expires_at = claim_expiry(
        resources, resolved_run_id, namespace=resolved_namespace, deadline_seconds=deadline_seconds
    )
    claim_run(
        resources, resolved_run_id, namespace=resolved_namespace, expires_at=expires_at, now=now
    )
    artifacts = RunArtifacts(artifacts_root or ARTIFACTS_ROOT, str(resolved_run_id))
    node_exporter: NodeExporter | None = None
    # The run's managed Prometheus, once provisioned: phase 10 checks it is still up.
    prometheus: ManagedPrometheus | None = None
    # The pairs whose pods were deployed: phase 10 expects only their pods to exist.
    started: set[str] = set()
    try:
        console.say(
            "phase 4 (preflight): cluster claimed for this run until "
            f"{format_timestamp(expires_at)}"
        )

        # ADR-03-02: deployed and verified before any workload, and said prominently,
        # so the user always knows which Prometheus produced their results.
        ownership = RunOwnership(resources, resolved_run_id)
        supporting = detect_supporting_exporters(resources)
        node_exporter = provide_node_exporter(
            ownership,
            resolved_namespace,
            supporting,
            image=run_images["node-exporter"].reference,
        )
        prometheus = provision_managed_prometheus(
            ownership,
            resolved_namespace,
            supporting,
            selection=monitoring,
            node_exporter=node_exporter,
            avoid_nodes=_tested_nodes(plan),
            image=run_images["prometheus"].reference,
        )
        console.say(f"phase 4 (preflight): monitoring mode {monitoring.describe()}")
        console.say(f"  Prometheus  {prometheus.endpoint}")
        for key, value in prometheus.run_metadata().items():
            if key.startswith("supporting_"):
                console.say(f"  {key.removeprefix('supporting_'):<18}  {value or 'absent'}")
        verify_query_access(
            PrometheusClient(prometheus.endpoint), timeout=QUERY_ACCESS_TIMEOUT_SECONDS
        )
        console.say("phase 4 (preflight): Prometheus answers queries")
        if node_exporter is not None:
            # Read after the query check, which gives the DaemonSet time to place its pods.
            running, summary = node_exporter_status(resources, node_exporter)
            console.say(f"  node-exporter pods  {summary}")
            if not running:
                console.warn(f"node-exporter {node_exporter.name}: {summary}")
        console.say(
            "  kept after the run (ADR-03-02); remove it with: " + removal_command(prometheus)
        )
        console.say("  query it from your machine with: " + port_forward_command(prometheus))

        # ADR-05-07: the cluster checks, against the nodes as they are now rather than at
        # planning, and the first pair in probe mode. A failure raises and exits 4.
        report = check_cluster(
            definition,
            plan,
            discover(cluster),
            resources=resources,
            run_id=str(resolved_run_id),
            namespace=resolved_namespace,
            cluster=run_cluster,
            images=run_images,
            plugin=PREFLIGHT_PLUGIN,
        )
        console.say(f"phase 4 (preflight): nodes {', '.join(report.nodes)} accept test pods")
        console.say(
            f"phase 4 (preflight): {report.pair.test_id} scheduled, pulled its image and "
            f"connected, as {', '.join(sorted(report.pods.values()))}"
        )
        console.say(f"  {report.probe}")
        if node_exporter is not None or any(
            exporter.kind == NODE_EXPORTER_COMPONENT for exporter in supporting
        ):
            # Late in the phase, so that Prometheus has had its first scrapes. A warning:
            # node metrics explain a result, they never decide one (ADR-03-02).
            unreachable = node_exporter_reachability(
                PrometheusClient(prometheus.endpoint), NODE_EXPORTER_COMPONENT
            )
            if unreachable is not None:
                console.warn(
                    f"node-exporter: {unreachable}. The run continues without node metrics"
                )
        # The probe is never next to a test: its pods go before phase 5, and phase 6
        # deploys the pair afresh.
        if not _remove_test_pods(
            console, resources, resolved_run_id, resolved_namespace, phase="phase 4 (preflight)"
        ):
            raise ClusterError(
                "the preflight's probe pods could not be removed, so no test starts next to them"
            )
        # A phase line, so on the same stream as the others: stdout and stderr reach the
        # Job's log through separate pipes, and lines microseconds apart can swap.
        console.say("phase 4 (preflight): the cluster is ready")
        console.say()
        if preflight_only:
            console.say("preflight only: the run stops before any test, as asked")
            return ExitCode.PASS

        suite = render_suite(definition, artifacts)
        console.say(f"phase 5 (suite rendering): {suite}")
        executions = execute_pairs(
            plan,
            suite=suite,
            artifacts=artifacts,
            runner=ROBOT_RUNNER(),
            robot_directory=robot_directory or default_robot_directory(),
            namespace=resolved_namespace,
            cluster=run_cluster,
            images=run_images,
            on_pair=lambda pair: _start_pair(console, pair),
            after_pair=lambda pair: _record_deployed(
                console, resources, resolved_run_id, resolved_namespace, started, pair
            ),
        )
        _confirm_measurements(console, prometheus, executions, artifacts)
        merge_reports(executions, artifacts, ROBOT_RUNNER())
        console.say(f"phase 9a (reporting): {artifacts.log}, {artifacts.report}")
        console.say()
        console.warn(
            "the rest of phases 8 to 11 is not implemented in this build: validation "
            "(#48), results (#49), cleanup and outcome (#51). The run stops here without "
            "a verdict; Robot's report says which steps ran."
        )
        return ExitCode.INTERNAL
    finally:
        # Phase 10 first, on every path: while everything the run created still exists,
        # and before cleanup removes any of it (ADR-05-07).
        _diagnose(
            console,
            resources,
            cluster,
            run_cluster,
            resolved_run_id,
            resolved_namespace,
            plan,
            started,
            artifacts,
            prometheus,
        )
        # ADR-05-13: a run stops blocking the cluster only once nothing of it can still be
        # sending traffic. A test aborted mid-measurement leaves a client that keeps
        # sending, so its pods go first, on every path, and the claim is released only
        # once they are gone. If they cannot be removed, the claim stays until it expires.
        # The artifacts go last in the log, so they come after the pods' removal, and the
        # claim is released after both.
        removed = _remove_test_pods(console, resources, resolved_run_id, resolved_namespace)
        if node_exporter is not None:
            # The 2026-10-07 decision: a run removes the node-exporter it deployed, before
            # it releases the cluster, and leaves one it found.
            _remove_node_exporter(console, resources, node_exporter)
        if not removed:
            # Removing the pods alone frees nothing: only a finished or expired claim does
            # (ADR-05-13). So the way out names both steps, the second one being the
            # recovery the refusal of a later run prints too.
            console.warn(
                f"run {resolved_run_id} keeps its claim on the cluster until "
                f"{format_timestamp(expires_at)}, because its test pods may still be sending "
                "traffic. To free it earlier, remove them: kubectl delete pod -n "
                f"{resolved_namespace} -l '{_test_pod_selector(resolved_run_id)}'. "
                "Then clear the claim by "
                "deleting the run's owner, which removes the rest of the run with it: "
                f"kubectl delete configmap -n {resolved_namespace} "
                f"{owner_name(resolved_run_id)}"
            )
        _return_artifacts(console, artifacts)
        if removed:
            _finish(console, resources, resolved_run_id, resolved_namespace, now)


MEASUREMENT_REPORT = "measurement.json"


def _confirm_measurements(
    console: Console,
    prometheus: ManagedPrometheus,
    executions: Sequence[PairExecution],
    artifacts: RunArtifacts,
) -> None:
    """Phase 7, the Job's half: does Prometheus hold what each test's direct read found?

    Before teardown, while the exporters still serve (ADR-03-01). A test whose series
    never arrive is a warning naming it, and a value that differs is a diagnostic of its
    own; neither changes the verdict, which is the direct read's. Never raises.
    """
    measurements: list[Measurement] = []
    for execution in executions:
        found = read_measurement(execution.output.parent, execution.pair.test_id)
        if found is None:
            console.warn(
                f"phase 7 (measurement): {execution.pair.test_id} has no measurement: its "
                "test ended before its result was read, and Robot's report says why"
            )
        else:
            measurements.append(found)
    if not measurements:
        return
    for measurement in measurements:
        values = ", ".join(
            f"{name.removeprefix('dokimi_')} {value:g}"
            for name, value in sorted(measurement.values.items())
        )
        console.say(
            f"phase 7 (measurement): {measurement.test_id}, read directly from its "
            f"exporters ({measurement.direction}): {values}"
        )
    try:
        report = confirm_scrape(
            PrometheusClient(prometheus.endpoint).query,
            measurements,
            scrape_interval=SCRAPE_INTERVAL_SECONDS,
            timeout=SCRAPE_CONFIRMATION_TIMEOUT_SECONDS,
        )
        _report_scrape(console, report)
        artifacts.directory.mkdir(parents=True, exist_ok=True)
        (artifacts.directory / MEASUREMENT_REPORT).write_text(
            report.to_json(measurements), encoding="utf-8"
        )
    except Exception as error:  # the verdict is the direct read's, whatever happens here
        console.warn(f"phase 7 (measurement): could not confirm the scrape: {error}")
    console.say()


def _report_scrape(console: Console, report: ScrapeReport) -> None:
    if report.confirmed:
        console.say(
            "phase 7 (measurement): Prometheus holds the series of "
            + ", ".join(sorted(report.confirmed))
        )
    if report.missing:
        console.warn(
            f"HISTORY NOT CAPTURED for {', '.join(report.missing)}: Prometheus did not hold "
            f"its series after {report.timeout:g}s"
            + (f" ({report.error})" if report.error else "")
            + ". The verdict comes from the direct read and stands; only the history in "
            "Prometheus is missing"
        )
    for disagreement in report.disagreements:
        console.warn(
            f"phase 7 (measurement): {disagreement.test_id} {disagreement.metric}: the direct "
            f"read found {disagreement.direct!r} and Prometheus holds "
            f"{disagreement.prometheus!r}. The verdict uses the direct read; the difference "
            "is a delivery problem worth a look"
        )


#: A test's pods, by their component label. Only these are removed before the claim is
#: released: a run's other pods, such as its kept Prometheus or a DaemonSet's, are not test
#: traffic, and a controller would only recreate them.
TEST_POD_COMPONENTS = frozenset(role.value for role in Role)


def _test_pod_selector(run_id: RunId) -> str:
    """The label selector for a run's test pods, the same set phase 11 removes."""
    components = ",".join(sorted(TEST_POD_COMPONENTS))
    return f"{RUN_ID_LABEL}={run_id},{COMPONENT_LABEL} in ({components})"


#: How long phase 11 waits for the run's test pods to be gone: their grace period, plus
#: the API server's and the kubelet's reaction time.
POD_REMOVAL_TIMEOUT_SECONDS = 60.0


def _start_pair(console: Console, pair: PlannedPair) -> None:
    console.say(
        f"phase 6 (execution): {pair.test_id}  {pair.source.name} -> "
        f"{pair.target.name}, one Robot Framework process"
    )


def _record_deployed(
    console: Console,
    resources: ResourceClient,
    run_id: RunId,
    namespace: str,
    started: set[str],
    pair: PlannedPair,
) -> None:
    """After a pair's Robot process: whether its pods were deployed, for phase 10.

    Only deploy() creates a test's pods, inside the Robot process, and nothing removes
    them before phase 11. So a pod of the pair now means its pods were deployed, and none
    means it ended before that, by an error its own category names. A pair that could not
    be checked counts as deployed: a missing pod is then reported rather than missed.
    """
    # Only a test's pods carry its test id.
    selector = f"{run_selector(run_id)},{TEST_ID_LABEL}={pair.test_id}"
    try:
        deployed = bool(resources.list(POD, selector, namespace))
    except Exception as error:
        console.warn(f"could not check whether {pair.test_id}'s pods were deployed: {error}")
        deployed = True
    if deployed:
        started.add(pair.test_id)


def _diagnose(
    console: Console,
    resources: ResourceClient,
    cluster: ClusterClient,
    cluster_name: str,
    run_id: RunId,
    namespace: str,
    plan: Plan,
    started: set[str],
    artifacts: RunArtifacts,
    prometheus: ManagedPrometheus | None,
) -> None:
    """Phase 10: the post-run snapshot, written next to Robot's reports. Never raises.

    Should anything in it raise after all, the run would keep its test pods and its claim,
    so whatever it is becomes a warning, and cleanup goes on.
    """
    try:
        ready = None if prometheus is None else PrometheusClient(prometheus.endpoint).ready
        diagnostics = capture(
            resources,
            run_id=run_id,
            namespace=namespace,
            plan=plan,
            started=started,
            artifacts=artifacts,
            list_nodes=cluster.list_nodes,
            prometheus_ready=ready,
            cluster=cluster_name,
            timeout=DIAGNOSTICS_TIMEOUT_SECONDS,
        )
        for line in diagnostics.summary():
            console.say(line)
        report = write_report(diagnostics, artifacts.directory)
        console.say(f"phase 10 (diagnostics): {report}")
    except Exception as error:
        console.warn(f"could not record the run's diagnostics: {error}")
    console.say()


def _remove_test_pods(
    console: Console,
    resources: ResourceClient,
    run_id: RunId,
    namespace: str,
    *,
    phase: str = "phase 11 (cleanup)",
    timeout: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Delete the run's test pods, and wait until they are gone. True if they are.

    Only the test's client and server pods: the owner and the definition stay for
    ``attach`` and for cleanup (M1-16), and the Job's own pod is never selected. Waiting
    matters, because a deleted pod keeps running through its grace period, and so can its
    traffic.
    """
    limit = POD_REMOVAL_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        report = delete_run_owned(
            resources, run_id, namespace=namespace, kinds=(POD,), components=TEST_POD_COMPONENTS
        )
        if report.failed:
            console.warn(
                f"could not delete run {run_id}'s test pods: "
                + "; ".join(f"{obj.name}: {error}" for obj, error in report.failed)
            )
            return False
        deadline = clock() + limit
        while find_run_owned(
            resources, run_id, namespace=namespace, kinds=(POD,), components=TEST_POD_COMPONENTS
        ):
            if clock() >= deadline:
                console.warn(f"run {run_id}'s test pods were still there after {limit:g}s")
                return False
            sleep(2.0)
    except ClusterError as error:
        console.warn(f"could not remove run {run_id}'s test pods: {error}")
        return False
    if report.deleted:
        console.say(f"{phase}: removed {len(report.deleted)} test pod(s)")
    return True


def _remove_node_exporter(
    console: Console, resources: ResourceClient, exporter: NodeExporter
) -> None:
    """Remove the node-exporter this run deployed. Never fails the run: it is not traffic."""
    try:
        if remove_node_exporter(resources, exporter):
            console.say(f"phase 11 (cleanup): removed node-exporter {exporter.name}")
    except ClusterError as error:
        # A later run finds it and reuses it, so nothing clashes on the host port.
        console.warn(f"could not remove node-exporter {exporter.name}: {error}")


def _finish(
    console: Console,
    resources: ResourceClient,
    run_id: RunId,
    namespace: str,
    now: Callable[[], datetime],
) -> None:
    """The start of phase 11: record that the run no longer holds the cluster."""
    try:
        mark_finished(resources, run_id, namespace=namespace, now=now)
    except ClusterError as error:
        # Never mask the run's own outcome. The claim still ends when it expires.
        console.warn(
            f"could not mark run {run_id} finished; its claim ends when it expires: {error}"
        )


def _return_artifacts(console: Console, artifacts: RunArtifacts) -> None:
    """Write the run's artifact directory into the log, last, for the CLI to extract.

    Also after a failed run: whatever was written is worth having. Never at the cost of
    the run's own outcome (ADR-05-15, provisional).
    """
    if not artifacts.directory.is_dir():
        return
    # The block goes to stdout and the notes about it to stderr, and the CLI reads both
    # from one log. Flushed on both sides, so neither lands inside the block, whatever
    # the buffering of the process.
    console.flush()
    try:
        size = write_artifacts(artifacts.directory, artifacts.run_id, console.say)
    except OSError as error:
        console.flush()
        console.warn(f"could not return the run's artifacts in its log: {error}")
        return
    console.flush()
    console.warn(f"artifacts: {size} bytes in the log above, for submit and attach to extract")


def show_image(
    console: Console, *, image_override: str | None = None, images: Sequence[str] = ()
) -> int:
    """Show the images a run would submit and deploy, and where each one came from.

    A diagnostic, so a component without an image is listed as such rather than stopping
    the listing: ``submit`` is what refuses it.
    """
    overrides = parse_image_options(images, orchestrator_image=image_override)
    width = max(len(name) for name in COMPONENTS) + 2
    for component in COMPONENTS.values():
        run_image = resolve_component(component, overrides.get(component.name))
        if run_image is None:
            console.say(f"{component.name:<{width}}no default in a development build")
            console.say(
                f"  choose one with --image {component.name}=<reference> or {component.env_var}"
            )
            continue
        console.say(f"{component.name:<{width}}{run_image.reference}")
        console.say(f"  source: {run_image.source}")
        console.say(f"  digest: {run_image.digest or 'not pinned by digest'}")
    return ExitCode.PASS


def _follow(
    console: Console,
    cluster: ClusterClient,
    namespace: str,
    run_id: str,
    results: Path,
    *,
    expect_artifacts: bool = True,
) -> int:
    """Stream the run, keep its artifacts out of what is printed, and extract them.

    Extracting never changes the run's exit code: the artifacts are reported, the
    verdict is the Job's.
    """
    receiver = ArtifactReceiver(run_id)
    ended = False

    def write(line: str) -> None:
        for printed in receiver.feed(line):
            console.say(printed)

    try:
        code = follow(cluster, namespace=namespace, run_id=run_id, write=write)
        ended = True
        return code
    except KeyboardInterrupt:
        # ADR-01-06: the Job is a server-side resource. Leaving is not stopping.
        console.warn("")
        console.warn(
            f"stopped following run {run_id}. The run is unaffected and continues in the "
            f"cluster; resume with: dokimi attach {run_id}"
        )
        return ExitCode.INTERRUPTED
    finally:
        # A preflight writes no test results, only phase 10's snapshot, so missing
        # artifacts are not worth a note there.
        _store_artifacts(console, receiver, results / run_id, ended=ended and expect_artifacts)


def _store_artifacts(
    console: Console, receiver: ArtifactReceiver, destination: Path, *, ended: bool
) -> None:
    """Extract what arrived, or say why nothing did. Never raises: the exit code is the run's."""
    if receiver.received:
        try:
            receiver.extract(destination)
        except ArtifactStreamError as error:
            console.warn(f"the run's artifacts were not extracted: {error}")
            return
        except OSError as error:
            console.warn(f"could not write the run's artifacts to {destination}: {error}")
            return
        console.warn(f"artifacts: {destination}")
    elif receiver.error is not None:
        console.warn(f"the run's artifacts were not extracted: {receiver.error}")
    elif receiver.incomplete:
        console.warn(
            "the run's artifacts were cut off before the end of its log. Reattach while "
            f"the run's pod still exists to fetch them: dokimi attach {receiver.run_id}"
        )
    elif ended:
        console.warn(
            "the run's log carried no artifacts: it stopped before writing any, or its log "
            "was rotated or cut. If the run got as far as its tests, reattach while its pod "
            f"still exists: dokimi attach {receiver.run_id}"
        )


def _results_directory_of(
    console: Console, cluster: ClusterClient, namespace: str, run_id: str
) -> Path:
    """The run's ``results.directory``, read from the definition it was submitted with.

    ``attach`` never saw the document, but the run carries it in a ConfigMap. If that
    cannot be read, the default it would have had.
    """
    try:
        config_map = cluster.resources().get(
            CONFIG_MAP, namespace, resource_name(run_id, "-definition")
        )
        (name, source), *_ = (config_map.get("data") or {}).items()
        definition = parse(source, SUFFIXES[Path(name).suffix])
    except (NotFoundError, ClusterError, ConfigError, KeyError, ValueError) as error:
        console.warn(
            f"could not read run {run_id}'s results directory ({error}); "
            f"using {DEFAULT_RESULTS_DIRECTORY}"
        )
        return Path(DEFAULT_RESULTS_DIRECTORY)
    return Path(definition.results_directory)


def _render(
    console: Console,
    definition: TestDefinition,
    submission: Submission,
    plan: Plan | None,
) -> int:
    """``--dry-run``: the planned matrix and the manifests, and nothing created.

    The manifests go to stdout and everything a person reads goes to stderr, so the
    rendering can be diffed or saved without a summary mixed into it.

    With *plan* the matrix shown is the resolved one, checked read-only against a
    cluster. Without it, it is the pair the definition asks for, and the output says the
    nodes were not checked rather than implying they were.
    """
    if plan is not None:
        console.warn(f"Planned matrix ({_plan_summary(plan)})")
        console.warn("")
        for line in _plan_lines(plan):
            console.warn(line)
    else:
        console.warn("Planned matrix (1 test, sequential; nodes not checked)")
        console.warn("")
        console.warn(f"  {definition.source_node} -> {definition.target_node}")
    console.warn(f"    type       {definition.test_type.value} over {definition.protocol.value}")
    console.warn(f"    duration   {definition.duration}")
    console.warn(f"    bandwidth  {definition.target_bandwidth}")
    console.warn("")
    if plan is not None:
        console.warn("Both nodes exist and can accept a test pod.")
    else:
        console.warn(
            "Nodes were not checked against a cluster. A dry run contacts nothing unless "
            "given --kubeconfig or --context, and then only to read."
        )
    console.warn("")
    console.warn(f"Run id     {submission.run_id}")
    console.warn(f"Namespace  {submission.namespace}")
    console.warn(f"Cluster    {submission.cluster}")
    console.warn("Images")
    width = max((len(name) for name in submission.images), default=0) + 2
    for name, run_image in sorted(submission.images.items()):
        console.warn(f"  {name:<{width}}{run_image.reference} ({run_image.source})")
    console.warn(f"Deadline   {job_deadline_seconds(definition)}s")
    console.warn("")

    console.say("---")
    for manifest in submission.manifests:
        console.say(_dump(manifest))
        console.say("---")

    console.warn("dry run: nothing was created.")
    console.warn(
        "The owner uid is empty above because the API server assigns it; submission "
        "fills it in before creating anything that references it."
    )
    return ExitCode.PASS


class _ReadableDumper(yaml.SafeDumper):
    """A dumper that renders manifests the way a person would write them.

    Two departures from the default, both about the same thing — a dry run is read by
    someone deciding whether to submit:

    * Multi-line strings use a literal block. The authored document is embedded in a
      ConfigMap, and the default style turns it into one escaped line.
    * Anchors and aliases are off. The Job's labels are the same object as its pod
      template's, so the default emits the second one as ``*id001``.
    """

    def ignore_aliases(self, data: object) -> bool:
        return True


def _literal_string(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_ReadableDumper.add_representer(str, _literal_string)


def _dump(manifest: dict[str, Any]) -> str:
    return yaml.dump(
        manifest,
        Dumper=_ReadableDumper,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    ).rstrip()


def _describe_images(console: Console, run_images: Mapping[str, WorkloadImage]) -> None:
    for name, run_image in sorted(run_images.items()):
        console.say(f"  image       {name} {run_image.reference} ({run_image.source})")


def _describe(console: Console, definition: TestDefinition) -> None:
    console.say(f"  name        {definition.name}")
    console.say(f"  pair        {definition.source_node} -> {definition.target_node}")
    console.say(f"  test        {definition.test_type.value} over {definition.protocol.value}")
    console.say(f"  duration    {definition.duration}")
    console.say(f"  bandwidth   {definition.target_bandwidth}")
    console.say(f"  monitoring  {definition.monitoring_mode.value}")
    console.say(f"  results     {definition.results_directory}")
    console.say(f"  criteria    {len(definition.criteria)}")
    for criterion in definition.criteria:
        console.say(f"    - {_phrase(criterion)}")


def _phrase(criterion: Criterion) -> str:
    """One criterion on one line, in the vocabulary the document used."""
    statistic = f"{criterion.statistic.value} " if criterion.statistic is not None else ""
    return (
        f"{statistic}{criterion.metric.value} {criterion.comparison.value} "
        f"{criterion.threshold} {criterion.unit.value}"
    )


def _plan_summary(plan: Plan) -> str:
    tests = "1 test" if len(plan.pairs) == 1 else f"{len(plan.pairs)} tests"
    return f"{tests}, {plan.concurrency.value}"


def _tested_nodes(plan: Plan) -> list[str]:
    return sorted({node.name for pair in plan.pairs for node in (pair.source, pair.target)})


def _plan_lines(plan: Plan) -> list[str]:
    return [f"  {pair.test_id}  {pair.source.name} -> {pair.target.name}" for pair in plan.pairs]
