"""What each CLI command does, independent of how its arguments were parsed.

Phase 1 and nothing else. Everything here either runs entirely locally or talks to the
API server; no phase of a run happens on this side of the cluster boundary (ADR-01-06).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import yaml

from dokimi.artifact_stream import ArtifactReceiver, ArtifactStreamError, write_artifacts
from dokimi.cleanup import POD_REMOVAL_TIMEOUT_SECONDS as DEFAULT_POD_REMOVAL_TIMEOUT
from dokimi.cleanup import (
    PodRemoval,
    Release,
    Retention,
    RunCleanup,
    clean_up_run,
    quiet_tests,
    release_fetched_run,
    remove_test_pods,
    runs_older_than,
)
from dokimi.cli.exit_codes import ExitCode
from dokimi.cluster_name import cluster_from_job_environment, resolve_cluster
from dokimi.diagnostics import SNAPSHOT_TIMEOUT_SECONDS, capture, write_report
from dokimi.error_categories import ErrorCategory
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
from dokimi.k8s.identity import validate_run_id
from dokimi.k8s.manifests import job_deadline_seconds, resource_name
from dokimi.k8s.namespaces import namespace_for
from dokimi.k8s.ownership import (
    TEST_ID_LABEL,
    OwnershipError,
    RunOwnership,
    format_timestamp,
    run_selector,
)
from dokimi.k8s.resources import CONFIG_MAP, POD, NotFoundError, ResourceClient
from dokimi.k8s.submission import Submission, follow, plan_submission, submit
from dokimi.measurement import Measurement, ScrapeReport, confirm_scrape, read_measurement
from dokimi.metrics.managed import (
    NODE_EXPORTER_COMPONENT,
    SCRAPE_INTERVAL_SECONDS,
    ManagedPrometheus,
    MonitoringSelection,
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
from dokimi.model import (
    DEFAULT_RESULTS_DIRECTORY,
    ConfigError,
    Criterion,
    Duration,
    TestDefinition,
    TestType,
)
from dokimi.planning import Plan, PlannedPair, discover, plan_run
from dokimi.preflight import check_cluster, node_exporter_reachability
from dokimi.results import (
    ASSERTIONS_CSV,
    ASSERTIONS_JSONL,
    RUN_RECORD,
    TESTS_CSV,
    TESTS_JSONL,
    RunIdentity,
    records_for_assertions,
    records_for_tests,
    write_records,
    write_run_record,
)
from dokimi.run_identity import RunId
from dokimi.run_images import (
    COMPONENTS,
    images_from_job_environment,
    parse_image_options,
    resolve_component,
    resolve_run_images,
)
from dokimi.testtypes.interface import WorkloadImage
from dokimi.testtypes.probe import Probes
from dokimi.validation import (
    Outcome,
    TestOutcome,
    format_value,
    read_outcome,
    run_outcome,
    worst,
)

__all__ = [
    "Console",
    "attach",
    "clean_up",
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
    retention: Retention = Retention.ON_FAILURE,
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
        retention=retention,
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


def clean_up(
    console: Console,
    *,
    run_id: str | None = None,
    older_than: str | None = None,
    namespace: str | None = None,
    prometheus: bool = False,
    force: bool = False,
    dry_run: bool = False,
    kubeconfig: str | None = None,
    context: str | None = None,
    connect_to: Callable[..., ClusterClient] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Remove a run, or every run older than an age, by the labels on what it created.

    Exits 0 once nothing is left, 4 if the run named by its id can't be removed now
    because it, or a run using what it left, holds the cluster, and 5 if something could
    not be removed, a refused or timed-out call included. With *older_than*, a run that
    can't be removed now is skipped, and the closing line counts them: it is not this
    command's to stop a run.
    """
    if (run_id is None) == (older_than is None):
        raise ConfigError("dokimi cleanup takes a run id or --older-than, and not both")
    if force and older_than is not None:
        raise ConfigError(
            "--force removes a run that still holds the cluster, so it takes that run's id, "
            "not --older-than"
        )
    resolved_namespace = namespace_for(TestType.IPERF2.value, namespace)
    # Both checked before any cluster call: a malformed one is a config_error.
    selection = RunId(validate_run_id(str(run_id))) if older_than is None else _age(older_than)
    resources = (connect_to or connect)(kubeconfig=kubeconfig, context=context).resources()
    if isinstance(selection, RunId):
        run_ids = [selection]
    else:
        run_ids = runs_older_than(resources, resolved_namespace, selection, now=now)
        if not run_ids:
            console.say(f"no run in {resolved_namespace} started more than {older_than} ago")
            return ExitCode.PASS
    flags = [
        "--namespace",
        resolved_namespace,
        *(["--prometheus"] if prometheus else []),
        *(["--force"] if force else []),
    ]
    code = ExitCode.PASS
    skipped = 0
    for each in run_ids:
        retry = " ".join(["dokimi cleanup", str(each), *flags])
        try:
            result = clean_up_run(
                resources,
                each,
                resolved_namespace,
                remove_prometheus=prometheus,
                force=force,
                dry_run=dry_run,
                pod_timeout=POD_REMOVAL_TIMEOUT_SECONDS,
                now=now,
            )
        except ClusterError as error:
            # One run's failure doesn't stop the others: each is removed on its own.
            console.warn(f"run {each}: could not remove it: {error}. Retry with: {retry}")
            code = max(code, ExitCode.INTERNAL)
            continue
        _report_cleanup(console, result, resolved_namespace, retry=retry, dry_run=dry_run)
        if result.refused is not None:
            skipped += 1
            if older_than is None:
                code = max(code, ExitCode.PREFLIGHT_FAILURE)
        elif not result.complete:
            code = max(code, ExitCode.INTERNAL)
    if older_than is not None and skipped:
        console.say(f"skipped {skipped} of {len(run_ids)} run(s), which can't be removed now")
    return code


def _age(text: str) -> timedelta:
    """``--older-than``, as a duration that a date can be counted back by."""
    try:
        age = timedelta(seconds=float(Duration.parse(text).seconds))
        # The date the age counts back to has to exist.
        _ = datetime.now(UTC) - age
    except OverflowError:
        raise ConfigError(f"--older-than {text} reaches back before any run could start") from None
    return age


def _report_cleanup(
    console: Console, result: RunCleanup, namespace: str, *, retry: str, dry_run: bool
) -> None:
    run_id = result.run_id
    if result.refused is not None:
        console.warn(f"run {run_id}: not removed: {result.refused}")
        return
    if dry_run:
        if not result.removable:
            console.say(f"run {run_id}: nothing to remove in {namespace}")
        else:
            console.say(f"run {run_id}: would remove {len(result.removable)} object(s)")
            for obj in result.removable:
                console.say(f"  {obj.kind}/{obj.name}")
    elif not result.removed and result.complete:
        console.say(f"run {run_id}: nothing to remove in {namespace}")
    else:
        console.say(f"run {run_id}: removed {len(result.removed)} object(s)")
        for obj in result.removed:
            console.say(f"  {obj.kind}/{obj.name}")
        if result.pods is not None and result.pods.problem is not None:
            # The owner stays, and with it a forced run's claim, until the pods are gone.
            console.warn(f"  {result.pods.problem}; the rest of the run stays until they go")
        for obj, error in [] if result.deletion is None else result.deletion.failed:
            console.warn(f"  could not remove {obj.kind}/{obj.name}: {error}")
        if not result.complete:
            console.warn(f"retry with: {retry}")
    if result.kept:
        console.say(
            f"  kept: {', '.join(f'{obj.kind}/{obj.name}' for obj in result.kept)}, the run's "
            "Prometheus and its history. Add --prometheus to remove it too"
        )


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
    retention: Retention = Retention.ON_FAILURE,
) -> int:
    """The in-cluster entry point: what the submitted Job runs, phases 2 to 11.

    The Job is invoked with the mounted definition and the run's id. Phases 2 and 3 parse
    and plan; a node that is missing or can't take a test pod is a config_error, with
    nothing created. Phase 4 claims the cluster first, so only one run at a time measures
    its network, and a refused claim exits 4. Then :func:`_preflight`, and, unless this
    is ``dokimi preflight`` (*preflight_only*), the tests (:func:`_run_tests`).

    Phases 10 and 11 run on every path once the cluster is claimed: the post-run
    snapshot, while everything still exists, then the cleanup (:func:`_end_run`).

    The exit code is the run's verdict: 0, 1 or 2, from the worst test outcome, and 2 for
    a snapshot that found something wrong around the tests. A run that stops on an
    exception exits through :func:`dokimi.cli.main.main`'s mapping instead.
    """
    resolved_run_id = RunId(validate_run_id(run_id))
    resolved_namespace = namespace_for(TestType.IPERF2.value, namespace)
    if deadline_seconds <= 0:
        raise ConfigError(f"--deadline-seconds must be positive, got {deadline_seconds}")

    console.say(f"run {run_id}")
    definition = parse_file(path)
    # Decided before any cluster call: a mode this build cannot honour is a config_error.
    monitoring = select_monitoring(definition.monitoring_mode)
    # Resolved by submit, never here: the submitter's environment does not reach the Job.
    # What the Job was given is what it runs, and the log says so.
    run_images = images_from_job_environment(env, monitoring=monitoring.deploys)
    run_cluster = cluster_from_job_environment(env)
    console.say("phase 2 (ingestion): definition parsed")
    console.say()
    _describe(console, definition)
    console.say(f"  cluster     {run_cluster}")
    _describe_images(console, run_images)
    console.say()

    # Phase 3. Discovery happens here, in the cluster, with the Job's own credentials:
    # this is the resolution the run is held to, and it happens before anything is
    # deployed, so a node that is missing or unschedulable is a config_error with nothing
    # to clean up.
    cluster = (connect_to or connect)()
    plan = plan_run(definition, discover(cluster))
    console.say(f"phase 3 (planning): {_plan_summary(plan)}")
    for line in _plan_lines(plan):
        console.say(line)
    console.say()

    # Phase 4 begins with the claim. The Job's deadline, its start time plus
    # activeDeadlineSeconds, is when the claim expires: by then the cluster has killed
    # this Job, so an expired claim is safe to ignore.
    resources = cluster.resources()
    expires_at = claim_expiry(
        resources, resolved_run_id, namespace=resolved_namespace, deadline_seconds=deadline_seconds
    )
    claim_run(
        resources, resolved_run_id, namespace=resolved_namespace, expires_at=expires_at, now=now
    )
    run = _Run(
        resources=resources,
        run_id=resolved_run_id,
        namespace=resolved_namespace,
        plan=plan,
        artifacts=RunArtifacts(artifacts_root or ARTIFACTS_ROOT, str(resolved_run_id)),
        expires_at=expires_at,
        definition=definition,
        images=run_images,
        cluster_name=run_cluster,
        retention=retention,
        preflight_only=preflight_only,
    )
    #: What the run came to; ``None`` while it hasn't, which is how an exception ends it.
    outcome: Outcome | None = None
    try:
        console.say(
            "phase 4 (preflight): cluster claimed for this run until "
            f"{format_timestamp(expires_at)}"
        )
        prometheus = _preflight(
            console, cluster, run, definition, monitoring, run_images, run_cluster
        )
        run.past_preflight = True
        if preflight_only:
            console.say("preflight only: the run stops before any test, as asked")
            outcome = Outcome.PASS
        else:
            run.tests = _run_tests(
                console, run, prometheus, definition, run_images, run_cluster, robot_directory
            )
            outcome = run_outcome(run.tests, anomaly=False)
    finally:
        # Phase 10 first, on every path: while everything the run created still exists,
        # and before cleanup removes any of it.
        sound = _diagnose(console, cluster, run_cluster, run)
        # A preflight's code says whether the cluster is ready, 0 or 4. Anything the
        # snapshot found is in its log and diagnostics.json, not in that answer.
        if not sound and outcome is not None and not preflight_only:
            outcome = worst([outcome, Outcome.ERROR])
        # A run that stopped in phase 4 has nothing to inspect: its probe pods always go,
        # and its diagnostics.json reaches the submitter with its log.
        retained = retention.reason(outcome) if run.past_preflight else None
        # An exception on its way out is why the run has no outcome: its record says so.
        stopped = sys.exc_info()[1]
        _end_run(
            console,
            run,
            retained,
            now,
            outcome=outcome,
            stopped=None if stopped is None else str(stopped),
        )
    return ExitCode.of(outcome)


@dataclass
class _Run:
    """A claimed run: what phases 4 to 9 leave for phases 10 and 11 to look at and remove."""

    resources: ResourceClient
    run_id: RunId
    namespace: str
    plan: Plan
    artifacts: RunArtifacts
    expires_at: datetime
    #: The node-exporter the run deployed or reused, if it has one.
    node_exporter: NodeExporter | None = None
    #: The run's managed Prometheus, once provisioned: phase 10 checks it is still up.
    prometheus: ManagedPrometheus | None = None
    #: The tests whose pods were deployed: phase 10 expects only their pods to exist.
    started: set[str] = field(default_factory=set)
    #: Each test's outcome, once phase 8 has read them.
    tests: tuple[TestOutcome, ...] = ()
    #: Whether phase 4 passed, so that the run got as far as its tests.
    past_preflight: bool = False
    #: What the run was asked to do, for its record (``run.json``).
    definition: TestDefinition | None = None
    images: Mapping[str, WorkloadImage] = field(default_factory=dict)
    cluster_name: str = ""
    retention: Retention = Retention.ON_FAILURE
    preflight_only: bool = False
    #: Prometheus's scrape targets by scrape pool, taken in phase 9b while they exist.
    scrape_targets: dict[str, dict[str, int]] | None = None


def _preflight(
    console: Console,
    cluster: ClusterClient,
    run: _Run,
    definition: TestDefinition,
    monitoring: MonitoringSelection,
    run_images: Mapping[str, WorkloadImage],
    run_cluster: str,
) -> ManagedPrometheus:
    """Phase 4 after the claim: the run's Prometheus, then the cluster checks.

    The Prometheus is deployed and verified before any workload, and said prominently, so
    the user always knows which Prometheus produced their results. Then the planned nodes
    are checked as they are now, and the first pair is deployed once in probe mode and
    removed again. A failed check raises, which exits 4, and no test runs.
    """
    resources = run.resources
    ownership = RunOwnership(resources, run.run_id)
    supporting = detect_supporting_exporters(resources)
    run.node_exporter = provide_node_exporter(
        ownership, run.namespace, supporting, image=run_images["node-exporter"].reference
    )
    if run.node_exporter is not None and not run.node_exporter.by_this_run:
        # A crashed run's: every later run's Prometheus scrapes every node through it
        # until it is removed.
        console.warn(
            f"node-exporter {run.node_exporter.name} was left behind by run "
            f"{run.node_exporter.run_id}, and this run uses it. Once this run has ended, "
            f"remove what that run left with: dokimi cleanup {run.node_exporter.run_id} "
            f"--namespace {run.namespace}"
        )
    prometheus = provision_managed_prometheus(
        ownership,
        run.namespace,
        supporting,
        selection=monitoring,
        node_exporter=run.node_exporter,
        avoid_nodes=_tested_nodes(run.plan),
        image=run_images["prometheus"].reference,
    )
    run.prometheus = prometheus
    console.say(f"phase 4 (preflight): monitoring mode {monitoring.describe()}")
    console.say(f"  Prometheus  {prometheus.endpoint}")
    for key, value in prometheus.run_metadata().items():
        if key.startswith("supporting_"):
            console.say(f"  {key.removeprefix('supporting_'):<18}  {value or 'absent'}")
    verify_query_access(PrometheusClient(prometheus.endpoint), timeout=QUERY_ACCESS_TIMEOUT_SECONDS)
    console.say("phase 4 (preflight): Prometheus answers queries")
    if run.node_exporter is not None:
        # Read after the query check, which gives the DaemonSet time to place its pods.
        running, summary = node_exporter_status(resources, run.node_exporter)
        console.say(f"  node-exporter pods  {summary}")
        if not running:
            console.warn(f"node-exporter {run.node_exporter.name}: {summary}")
    console.say("  kept after the run; remove it with: " + removal_command(prometheus))
    console.say("  query it from your machine with: " + port_forward_command(prometheus))

    report = check_cluster(
        definition,
        run.plan,
        discover(cluster),
        resources=resources,
        run_id=str(run.run_id),
        namespace=run.namespace,
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
    if run.node_exporter is not None or any(
        exporter.kind == NODE_EXPORTER_COMPONENT for exporter in supporting
    ):
        # Late in the phase, so that Prometheus has had its first scrapes. A warning:
        # node metrics explain a result, they never decide one.
        unreachable = node_exporter_reachability(
            PrometheusClient(prometheus.endpoint), NODE_EXPORTER_COMPONENT
        )
        if unreachable is not None:
            console.warn(f"node-exporter: {unreachable}. The run continues without node metrics")
    # The probe is never next to a test: its pods go before phase 5, and phase 6 deploys
    # the pair afresh.
    removal = remove_test_pods(
        resources, run.run_id, run.namespace, timeout=POD_REMOVAL_TIMEOUT_SECONDS
    )
    if not removal.complete:
        raise ClusterError(
            f"the preflight's probe pods could not be removed, so no test starts next to "
            f"them: {removal.problem}"
        )
    _say_removed(console, "phase 4 (preflight)", removal)
    # A phase line, so on the same stream as the others: stdout and stderr reach the
    # Job's log through separate pipes, and lines microseconds apart can swap.
    console.say("phase 4 (preflight): the cluster is ready")
    console.say()
    return prometheus


def _run_tests(
    console: Console,
    run: _Run,
    prometheus: ManagedPrometheus,
    definition: TestDefinition,
    run_images: Mapping[str, WorkloadImage],
    run_cluster: str,
    robot_directory: Path | None,
) -> tuple[TestOutcome, ...]:
    """Phases 5 to 9: render the suite, run it once per pair, and read what each test came to.

    Each pair's Robot process reads its measurement directly from the exporters (phase 7)
    and evaluates its criteria (phase 8), and writes both next to its output. Here the Job
    confirms Prometheus holds the same values, before anything is torn down, and reads
    each test's outcome back: what the library classified, never Robot's status.
    """
    suite = render_suite(definition, run.artifacts)
    console.say(f"phase 5 (suite rendering): {suite}")
    executions = execute_pairs(
        run.plan,
        suite=suite,
        artifacts=run.artifacts,
        runner=ROBOT_RUNNER(),
        robot_directory=robot_directory or default_robot_directory(),
        namespace=run.namespace,
        cluster=run_cluster,
        images=run_images,
        on_pair=lambda pair: _start_pair(console, pair),
        after_pair=lambda pair: _record_deployed(console, run, pair),
    )
    _confirm_measurements(console, prometheus, executions, run.artifacts)
    tests = tuple(
        read_outcome(execution.output.parent, execution.pair.test_id) for execution in executions
    )
    _report_validation(console, tests)
    merge_reports(executions, run.artifacts, ROBOT_RUNNER())
    console.say(f"phase 9a (reporting): {run.artifacts.log}, {run.artifacts.report}")
    _write_results(console, run, definition, executions, tests, prometheus)
    console.say()
    return tests


def _write_results(
    console: Console,
    run: _Run,
    definition: TestDefinition,
    executions: Sequence[PairExecution],
    tests: Sequence[TestOutcome],
    prometheus: ManagedPrometheus,
) -> None:
    """Phase 9b: the CSV and JSONL result files, from what each test itself recorded.

    The measurements are the ones each test's Robot process read and wrote, and the
    assertions are what its criteria's evaluation recorded: the values the verdict came
    from, never read back from Prometheus. Also takes Prometheus's scrape targets for the
    run's record, while they still exist. Never raises: the verdict doesn't depend on its
    files, and a run whose files couldn't be written says so.
    """
    try:
        measurements: dict[str, Measurement] = {}
        for execution in executions:
            found = read_measurement(execution.output.parent, execution.pair.test_id)
            if found is not None:
                measurements[found.test_id] = found
        identity = RunIdentity(str(run.run_id), run.cluster_name, run.run_id.started_at)
        test_rows = records_for_tests(
            identity,
            definition,
            run.plan,
            tests,
            measurements,
            _history(run.artifacts.directory / MEASUREMENT_REPORT),
        )
        assertion_rows = records_for_assertions(identity, tests)
        write_records(run.artifacts.directory, test_rows, assertion_rows)
    except Exception as error:  # the verdict stands, whatever happens here
        console.warn(f"phase 9b (results): could not write the result files: {error}")
        return
    console.say(
        f"phase 9b (results): {len(test_rows)} test(s) in {TESTS_CSV} and {TESTS_JSONL}, "
        f"{len(assertion_rows)} assertion(s) in {ASSERTIONS_CSV} and {ASSERTIONS_JSONL}"
    )
    run.scrape_targets = _scrape_targets(console, prometheus)


def _history(report: Path) -> dict[str, bool]:
    """Whether Prometheus came to hold each test's series, by test id, from phase 7's report."""
    if not report.is_file():
        return {}
    document = json.loads(report.read_text(encoding="utf-8"))
    return {
        str(test["test_id"]): bool(test["history_captured"])
        for test in document.get("tests", [])
        if "history_captured" in test
    }


def _scrape_targets(console: Console, prometheus: ManagedPrometheus) -> dict[str, dict[str, int]]:
    """How many of each scrape pool's targets were up or down, for the run's record.

    A supporting exporter counts as used only where its targets were up. Never raises: a
    Prometheus that can't answer here leaves the count out, with a warning.
    """
    counts: dict[str, dict[str, int]] = {}
    try:
        targets = PrometheusClient(prometheus.endpoint).targets()
    except Exception as error:  # a note for the record, never a failure
        console.warn(f"phase 9b (results): could not read Prometheus's scrape targets: {error}")
        return counts
    for target in targets:
        pool = counts.setdefault(target.scrape_pool, {"up": 0, "down": 0, "unknown": 0})
        health = target.health if target.health in pool else "unknown"
        pool[health] += 1
    return counts


MEASUREMENT_REPORT = "measurement.json"


def _report_validation(console: Console, tests: Sequence[TestOutcome]) -> None:
    """Phase 8, as each pair's Robot process decided it: one line per test and criterion.

    The Job only prints what the library classified, never Robot's status. A test that
    ended before its criteria says why, with its error category. Never raises: it reports.
    """
    for test in tests:
        if test.document is None:
            console.warn(f"phase 8 (validation): {test.test_id} error: {test.problem}")
            continue
        criteria = test.document.get("criteria", [])
        console.say(
            f"phase 8 (validation): {test.test_id} {test.outcome.value}, "
            f"{len(criteria)} criterion(s)"
        )
        error = test.error
        if error is not None:
            category = f" ({error.category})" if error.category else ""
            console.say(f"  error{category}  ended before its criteria: {error.detail}")
        elif not criteria:
            console.say("  error  nothing asserted: the test has no pass criterion")
        for criterion in criteria:
            measured = [pair["value"] for pair in criterion.get("pairs", []) if pair["value"]]
            threshold = Decimal(criterion["threshold"])
            found = (
                f": {format_value(Decimal(measured[0]), threshold=threshold)} {criterion['unit']}"
                if len(measured) == 1
                else ""
            )
            categories = criterion.get("categories") or []
            why = f" ({', '.join(categories)})" if categories else ""
            console.say(f"  {criterion['outcome']}{why}  {criterion['phrase']}{found}")
    console.say()


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


#: How long phase 11 waits for the run's test pods to be gone. Module-level so a test can
#: shorten it.
POD_REMOVAL_TIMEOUT_SECONDS = DEFAULT_POD_REMOVAL_TIMEOUT


def _start_pair(console: Console, pair: PlannedPair) -> None:
    console.say(
        f"phase 6 (execution): {pair.test_id}  {pair.source.name} -> "
        f"{pair.target.name}, one Robot Framework process"
    )


def _record_deployed(console: Console, run: _Run, pair: PlannedPair) -> None:
    """After a pair's Robot process: whether its pods were deployed, for phase 10.

    Only deploy() creates a test's pods, inside the Robot process, and nothing removes
    them before phase 11. So a pod of the pair now means its pods were deployed, and none
    means it ended before that, by an error its own category names. A pair that could not
    be checked counts as deployed: a missing pod is then reported rather than missed.
    """
    # Only a test's pods carry its test id.
    selector = f"{run_selector(run.run_id)},{TEST_ID_LABEL}={pair.test_id}"
    try:
        deployed = bool(run.resources.list(POD, selector, run.namespace))
    except Exception as error:
        console.warn(f"could not check whether {pair.test_id}'s pods were deployed: {error}")
        deployed = True
    if deployed:
        run.started.add(pair.test_id)


def _diagnose(console: Console, cluster: ClusterClient, cluster_name: str, run: _Run) -> bool:
    """Phase 10: the post-run snapshot, written next to Robot's reports. True if all was well.

    Never raises: cleanup must go on. A snapshot that could not be taken at all counts as
    one that found something, since nothing says the run was sound.
    """
    try:
        prometheus = run.prometheus
        ready = None if prometheus is None else PrometheusClient(prometheus.endpoint).ready
        diagnostics = capture(
            run.resources,
            run_id=run.run_id,
            namespace=run.namespace,
            plan=run.plan,
            started=run.started,
            artifacts=run.artifacts,
            list_nodes=cluster.list_nodes,
            prometheus_ready=ready,
            cluster=cluster_name,
            timeout=DIAGNOSTICS_TIMEOUT_SECONDS,
        )
        for line in diagnostics.summary():
            console.say(line)
        report = write_report(diagnostics, run.artifacts.directory)
        console.say(f"phase 10 (diagnostics): {report}")
    except Exception as error:
        console.warn(f"could not record the run's diagnostics: {error}")
        console.say()
        return False
    console.say()
    return not diagnostics.anomalies


def _end_run(
    console: Console,
    run: _Run,
    retained: str | None,
    now: Callable[[], datetime],
    *,
    outcome: Outcome | None = None,
    stopped: str | None = None,
) -> None:
    """Phase 11: remove what the run doesn't keep, return its artifacts, release the cluster.

    A run that keeps its evidence (*retained* says why) keeps the pods of each test known
    to be quiet; every other test pod goes, and is waited for, because the cluster is
    released next and a later run must not measure next to traffic. The node-exporter
    the run deployed always goes: the kept Prometheus would otherwise go on scraping every
    node. If a test pod can't be removed, the claim stays until it expires.

    Nothing here changes the exit code: a cleanup that fails is reported prominently, with
    what remains and how to remove it, and the verdict stands.
    """
    keep = quiet_tests(run.tests) if retained else frozenset[str]()
    removal = remove_test_pods(
        run.resources, run.run_id, run.namespace, keep=keep, timeout=POD_REMOVAL_TIMEOUT_SECONDS
    )
    _say_removed(console, "phase 11 (cleanup)", removal)
    if run.node_exporter is not None:
        _remove_node_exporter(console, run.resources, run.node_exporter)
    remove = f"dokimi cleanup {run.run_id} --namespace {run.namespace}"
    if not removal.complete:
        # Removing the pods alone frees nothing: only a finished or expired claim does.
        console.warn(
            f"phase 11 (cleanup): {removal.problem}. Run {run.run_id} keeps its claim on the "
            f"cluster until {format_timestamp(run.expires_at)}, because its test pods may "
            f"still be sending traffic. To free it earlier, once this Job has ended, remove "
            f"the run: {remove} --force"
        )
    elif retained:
        kept = ", ".join(sorted(pod.name for pod in removal.kept)) or "no test pods"
        console.say(
            f"phase 11 (cleanup): run kept for inspection ({retained}): {kept}, and its "
            f"owner and definition. It no longer holds the cluster. Remove it with: {remove}"
        )
    _record_run(console, run, outcome, retained, removal, now, stopped=stopped)
    _return_artifacts(console, run.artifacts)
    if removal.complete:
        _finish(console, run, retained, now)


def _record_run(
    console: Console,
    run: _Run,
    outcome: Outcome | None,
    retained: str | None,
    removal: PodRemoval,
    now: Callable[[], datetime],
    *,
    stopped: str | None = None,
) -> None:
    """``run.json``: the run's own record, last, once its verdict and cleanup are known.

    The effective configuration, the images it ran by digest, its plan, its monitoring and
    scrape targets, its outcome and exit code, what it kept, and whether its cleanup was
    complete. *outcome* ``None`` is a run that stopped on an error before it reached one,
    and *stopped* says which; its exit code is then the error's, which the CLI maps.
    Never raises: the record is returned with the artifacts if it could be written.
    """
    counts = {key.value: 0 for key in Outcome}
    for test in run.tests:
        counts[test.outcome.value] += 1
    definition = run.definition
    record: dict[str, Any] = {
        "run_id": str(run.run_id),
        "cluster": run.cluster_name,
        "namespace": run.namespace,
        "started_at": run.run_id.started_at,
        "ended_at": now(),
        "preflight_only": run.preflight_only,
        "outcome": None if outcome is None else outcome.value,
        "exit_code": None if outcome is None else int(ExitCode.of(outcome)),
        "stopped": stopped,
        "tests": {"planned": len(run.plan.pairs), **counts},
        "retained": retained,
        "cleanup": {
            "complete": removal.complete,
            "category": None if removal.complete else ErrorCategory.CLEANUP.value,
            "problem": removal.problem,
            "kept_pods": sorted(pod.name for pod in removal.kept),
        },
        "configuration": {
            "definition": None if definition is None else render_yaml(definition),
            "retain": run.retention.value,
            "namespace": run.namespace,
            "cluster": run.cluster_name,
        },
        "images": {
            name: {
                "reference": image.reference,
                "digest": image.digest,
                "source": image.source.value,
            }
            for name, image in sorted(run.images.items())
        },
        "plan": run.plan.run_metadata(),
        "monitoring": None if run.prometheus is None else run.prometheus.run_metadata(),
        "scrape_targets": run.scrape_targets,
    }
    try:
        write_run_record(run.artifacts.directory, record)
    except Exception as error:  # the record is worth having, never at the run's cost
        console.warn(f"could not write the run's record ({RUN_RECORD}): {error}")


def _say_removed(console: Console, phase: str, removal: PodRemoval) -> None:
    if removal.removed:
        console.say(f"{phase}: removed {len(removal.removed)} test pod(s)")


def _remove_node_exporter(
    console: Console, resources: ResourceClient, exporter: NodeExporter
) -> None:
    """Remove the node-exporter this run deployed. Never fails the run: it is not traffic."""
    try:
        if remove_node_exporter(resources, exporter):
            console.say(f"phase 11 (cleanup): removed node-exporter {exporter.name}")
    except ClusterError as error:
        # A later run finds it and reuses it, so nothing clashes on the host port.
        console.warn(f"phase 11 (cleanup): could not remove node-exporter {exporter.name}: {error}")


def _finish(console: Console, run: _Run, retained: str | None, now: Callable[[], datetime]) -> None:
    """Record that the run no longer holds the cluster, and whether it keeps its evidence."""
    try:
        mark_finished(
            run.resources, run.run_id, namespace=run.namespace, retained=retained, now=now
        )
    except (ClusterError, OwnershipError) as error:
        # Never mask the run's own outcome. The claim still ends when it expires.
        console.warn(
            f"could not mark run {run.run_id} finished; its claim ends when it expires: {error}"
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
    """Stream the run, keep its artifacts out of what is printed, extract them, release it.

    Once the artifacts are here, the run's Job has nothing left to give: it is removed, and
    with it whatever the run did not keep (:func:`_release`). Neither step changes the
    run's exit code: the artifacts are reported, the verdict is the Job's.
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
        settled = _store_artifacts(
            console, receiver, results / run_id, ended=ended and expect_artifacts
        )
        if ended and settled:
            _release(console, cluster, namespace, run_id)


def _store_artifacts(
    console: Console, receiver: ArtifactReceiver, destination: Path, *, ended: bool
) -> bool:
    """Extract what arrived, or say why nothing did.

    True unless reattaching could still fetch something: the artifacts are on disk, or
    the log carried none. Never raises: the exit code is the run's.
    """
    if receiver.received:
        try:
            receiver.extract(destination)
        except ArtifactStreamError as error:
            console.warn(f"the run's artifacts were not extracted: {error}")
            return False
        except OSError as error:
            console.warn(f"could not write the run's artifacts to {destination}: {error}")
            return False
        console.warn(f"artifacts: {destination}")
        return True
    if receiver.error is not None:
        console.warn(f"the run's artifacts were not extracted: {receiver.error}")
        return False
    if receiver.incomplete:
        console.warn(
            "the run's artifacts were cut off before the end of its log. Reattach while "
            f"the run's pod still exists to fetch them: dokimi attach {receiver.run_id}"
        )
        return False
    if ended:
        console.warn(
            "the run's log carried no artifacts: it stopped before writing any, or its log "
            "was rotated or cut. If the run got as far as its tests, reattach while its pod "
            f"still exists: dokimi attach {receiver.run_id}"
        )
    return True


def _release(console: Console, cluster: ClusterClient, namespace: str, run_id: str) -> None:
    """Remove the run's Job now that its artifacts are here, and the run if it kept nothing.

    Never raises: a Job that can't be removed here is removed by the cluster an hour
    after the run ended.
    """
    resolved = RunId(run_id)
    try:
        released = release_fetched_run(cluster.resources(), resolved, namespace)
    except ClusterError as error:
        console.warn(
            f"could not remove run {run_id}'s Job: {error}. The cluster removes it an hour "
            "after the run ended"
        )
        return
    if released is Release.RUN:
        console.warn(f"removed run {run_id} from the cluster, now that its artifacts are here")
    else:
        console.warn(
            f"removed run {run_id}'s Job, now that its artifacts are here. What the run kept "
            f"stays until: dokimi cleanup {run_id} --namespace {namespace}"
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
