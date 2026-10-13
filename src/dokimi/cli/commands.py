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
from dokimi.execution import (
    ARTIFACTS_ROOT,
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
    RunOwnership,
    delete_run_owned,
    find_run_owned,
    format_timestamp,
    owner_name,
)
from dokimi.k8s.resources import CONFIG_MAP, POD, NotFoundError, ResourceClient
from dokimi.k8s.submission import Submission, follow, plan_submission, submit
from dokimi.metrics.managed import (
    detect_supporting_exporters,
    node_exporter_status,
    provide_node_exporter,
    provision_managed_prometheus,
    removal_command,
    select_monitoring,
)
from dokimi.metrics.prometheus import (
    DEFAULT_QUERY_ACCESS_TIMEOUT_SECONDS,
    PrometheusClient,
    verify_query_access,
)
from dokimi.model import DEFAULT_RESULTS_DIRECTORY, ConfigError, Criterion, TestDefinition, TestType
from dokimi.planning import Plan, discover, plan_run
from dokimi.run_identity import RunId
from dokimi.run_images import (
    COMPONENTS,
    images_from_job_environment,
    parse_image_options,
    resolve_component,
    resolve_run_images,
)
from dokimi.testtypes.interface import Role, WorkloadImage

__all__ = ["Console", "attach", "convert", "execute", "show_image", "submit_run", "validate"]

#: How phase 6 runs Robot Framework. A module attribute so that a test can replace
#: the subprocess with a fake without a real Robot or a real cluster.
ROBOT_RUNNER: Callable[[], RobotRunner] = SubprocessRobotRunner

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
    service_account: str | None = None,
    kubeconfig: str | None = None,
    context: str | None = None,
    follow_logs: bool = True,
    connect_to: Callable[..., ClusterClient] | None = None,
) -> int:
    """Validate locally, then either render the run or create it.

    Every image is resolved here, before anything else happens: the orchestrator's, and
    the ones the run deploys (ADR-04-08). A source build given no override for one of them
    stops with a config_error, rather than a Job that fails once it is running.
    """
    definition = parse_file(path)
    # Refused before anything is created, a dry run included: a run that could only
    # fail in phase 2 should never be submitted (ADR-05-05's config_error).
    select_monitoring(definition.monitoring_mode)
    run_images = resolve_run_images(parse_image_options(images, orchestrator_image=image_override))
    submission = plan_submission(
        definition,
        source=path.read_text(encoding="utf-8"),
        source_path=path,
        image=run_images["orchestrator"].reference,
        images=run_images,
        run_id=run_id,
        namespace=namespace,
        service_account=service_account,
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
) -> int:
    """The in-cluster entry point: what the submitted Job runs.

    The CLI/Job boundary is one decision (ADR-01-06) and both of its sides belong to
    M1-04, so the contract is fixed here: the Job is invoked with the mounted definition
    and the run's id, and it owns phases 2 to 11.

    Phases 2 and 3 are implemented, and phase 4 begins with the run-exclusivity claim
    (ADR-05-10): a refused claim raises and exits 4 (ADR-01-11). Phase 4 then provisions
    the run's managed Prometheus, with a node-exporter when the cluster has none, and
    verifies it answers queries (ADR-03-02), before any workload exists; a Prometheus
    that cannot be queried exits 4 as well. Phases 5, 6 and 9a render the suite, run it
    through Robot Framework once per pair, and merge the reports into the run's artifact
    directory (ADR-01-08).

    Not built yet: preflight's checks (M1-07, #42), measurement (M1-12, #47), validation
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
    run_images = images_from_job_environment(env)
    console.say("phase 2 (ingestion): definition parsed")
    console.say()
    _describe(console, definition)
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
    try:
        console.say(
            "phase 4 (preflight): cluster claimed for this run until "
            f"{format_timestamp(expires_at)}"
        )

        # ADR-03-02: deployed and verified before any workload, and said prominently,
        # so the user always knows which Prometheus produced their results.
        ownership = RunOwnership(resources, resolved_run_id)
        supporting = detect_supporting_exporters(resources)
        node_exporter = provide_node_exporter(ownership, resolved_namespace, supporting)
        prometheus = provision_managed_prometheus(
            ownership,
            resolved_namespace,
            supporting,
            selection=monitoring,
            node_exporter=node_exporter,
            avoid_nodes=_tested_nodes(plan),
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
        # A phase line, so on the same stream as the others: stdout and stderr reach the
        # Job's log through separate pipes, and lines microseconds apart can swap.
        console.say(
            "phase 4 (preflight): the cluster checks are not implemented in this build "
            "(M1-07, #42); the run continues without them"
        )
        console.say()

        suite = render_suite(definition, artifacts)
        console.say(f"phase 5 (suite rendering): {suite}")
        executions = execute_pairs(
            plan,
            suite=suite,
            artifacts=artifacts,
            runner=ROBOT_RUNNER(),
            robot_directory=robot_directory or default_robot_directory(),
            namespace=resolved_namespace,
            images=run_images,
            on_pair=lambda pair: console.say(
                f"phase 6 (execution): {pair.test_id}  {pair.source.name} -> "
                f"{pair.target.name}, one Robot Framework process"
            ),
        )
        merge_reports(executions, artifacts, ROBOT_RUNNER())
        console.say(f"phase 9a (reporting): {artifacts.log}, {artifacts.report}")
        console.say()
        console.warn(
            "the rest of phases 7 to 11 is not implemented in this build: measurement "
            "(#47), validation (#48), results (#49), cleanup and outcome (#51). The run "
            "stops here without a verdict; Robot's report says which steps ran."
        )
        return ExitCode.INTERNAL
    finally:
        # ADR-05-13: a run stops blocking the cluster only once nothing of it can still be
        # sending traffic. A test aborted mid-measurement leaves a client that keeps
        # sending, so its pods go first, on every path, and the claim is released only
        # once they are gone. If they cannot be removed, the claim stays until it expires.
        # The artifacts go last in the log, so they come after the pods' removal, and the
        # claim is released after both.
        removed = _remove_test_pods(console, resources, resolved_run_id, resolved_namespace)
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


def _remove_test_pods(
    console: Console,
    resources: ResourceClient,
    run_id: RunId,
    namespace: str,
    *,
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
        console.say(f"phase 11 (cleanup): removed {len(report.deleted)} test pod(s)")
    return True


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
    console: Console, cluster: ClusterClient, namespace: str, run_id: str, results: Path
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
        _store_artifacts(console, receiver, results / run_id, ended=ended)


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
