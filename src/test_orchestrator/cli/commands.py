"""What each CLI command does, independent of how its arguments were parsed.

Phase 1 and nothing else. Everything here either runs entirely locally or talks to the
API server; no phase of a run happens on this side of the cluster boundary (ADR-01-06).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from test_orchestrator.cli.exit_codes import ExitCode
from test_orchestrator.formats import parse_file
from test_orchestrator.images import ImageResolutionError, resolve_image
from test_orchestrator.k8s.cluster import ClusterClient, connect
from test_orchestrator.k8s.identity import default_namespace, validate_run_id
from test_orchestrator.k8s.manifests import deadline_seconds
from test_orchestrator.k8s.submission import Submission, follow, plan_submission, submit
from test_orchestrator.model import ConfigError, Criterion, TestDefinition, TestType

__all__ = ["Console", "attach", "execute", "show_image", "submit_run", "validate"]


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
        print(f"test-orchestrator: {message}", file=self.err)


def validate(console: Console, *, path: Path) -> int:
    """Parse a definition and report what it says. Never touches a cluster."""
    definition = parse_file(path)
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
    service_account: str | None = None,
    kubeconfig: str | None = None,
    context: str | None = None,
    follow_logs: bool = True,
    connect_to: Callable[..., ClusterClient] | None = None,
) -> int:
    """Validate locally, then either render the run or create it."""
    definition = parse_file(path)
    image = _resolve_image(image_override)
    submission = plan_submission(
        definition,
        source=path.read_text(encoding="utf-8"),
        source_path=path,
        image=image,
        run_id=run_id,
        namespace=namespace,
        service_account=service_account,
    )

    if dry_run:
        _render(console, definition, submission)
        return ExitCode.PASS

    cluster = (connect_to or connect)(kubeconfig=kubeconfig, context=context)
    submit(cluster, submission)
    console.warn(f"submitted run {submission.run_id} to namespace {submission.namespace}")

    if not follow_logs:
        console.warn(f"not following. Reattach with: test-orchestrator attach {submission.run_id}")
        return ExitCode.PASS

    return _follow(console, cluster, submission.namespace, submission.run_id)


def attach(
    console: Console,
    *,
    run_id: str,
    namespace: str | None = None,
    kubeconfig: str | None = None,
    context: str | None = None,
    connect_to: Callable[..., ClusterClient] | None = None,
) -> int:
    """Resume streaming a run this process never submitted."""
    resolved_run_id = validate_run_id(run_id)
    # Milestone-01 runs one test type, so its namespace is derivable. Once a run can be
    # of another type, the namespace stops being guessable from nothing and --namespace
    # becomes the way to say which one.
    resolved_namespace = namespace or default_namespace(TestType.IPERF2)
    cluster = (connect_to or connect)(kubeconfig=kubeconfig, context=context)
    return _follow(console, cluster, resolved_namespace, resolved_run_id)


def execute(console: Console, *, path: Path, run_id: str) -> int:
    """The in-cluster entry point: what the submitted Job runs.

    The CLI/Job boundary is one decision (ADR-01-06) and both of its sides belong to
    M1-04, so the contract is fixed here: the Job is invoked with the mounted definition
    and the run's id, and it owns phases 2 to 11.

    Phase 2 is implemented, because parsing exists. The rest does not yet: planning is
    M1-06 (#41), preflight M1-07 (#42), execution M1-11 (#46), validation M1-13 (#48),
    results M1-14 (#49), cleanup and the real exit code M1-16 (#51). Rather than exit 0
    and let a submitter read that as a passing run, this reports an orchestrator error
    and names what is missing.
    """
    console.say(f"run {run_id}")
    definition = parse_file(path)
    console.say("phase 2 (ingestion): definition parsed")
    console.say()
    _describe(console, definition)
    console.say()
    console.warn(
        "phases 3 to 11 are not implemented in this build: planning (#41), preflight "
        "(#42), execution (#46), validation (#48), results (#49), cleanup and outcome "
        "(#51). The run stops here without a verdict."
    )
    return ExitCode.INTERNAL


def show_image(console: Console, *, image_override: str | None = None) -> int:
    """Show the orchestrator image a run would submit (M1-02)."""
    image = resolve_image(image_override)
    console.say(image.reference)
    console.say(f"  source: {image.source}")
    console.say(f"  digest: {image.digest or 'not pinned by digest'}")
    return ExitCode.PASS


def _follow(console: Console, cluster: ClusterClient, namespace: str, run_id: str) -> int:
    try:
        return follow(cluster, namespace=namespace, run_id=run_id, write=console.say)
    except KeyboardInterrupt:
        # ADR-01-06: the Job is a server-side resource. Leaving is not stopping.
        console.warn("")
        console.warn(
            f"stopped following run {run_id}. The run is unaffected and continues in the "
            f"cluster; resume with: test-orchestrator attach {run_id}"
        )
        return ExitCode.INTERRUPTED


def _resolve_image(override: str | None) -> str:
    try:
        return resolve_image(override).reference
    except ImageResolutionError as error:
        # Not having an image to submit is a problem with the request, not the cluster,
        # and it is detected before anything is created — a config_error by ADR-05-05.
        raise ConfigError(str(error)) from error


def _render(console: Console, definition: TestDefinition, submission: Submission) -> int:
    """``--dry-run``: the planned matrix and the manifests, and nothing created.

    The manifests go to stdout and everything a person reads goes to stderr, so the
    rendering can be diffed or saved without a summary mixed into it.
    """
    console.warn("Planned matrix (1 test)")
    console.warn("")
    console.warn(f"  {definition.source_node} -> {definition.target_node}")
    console.warn(f"    type       {definition.test_type.value} over {definition.protocol.value}")
    console.warn(f"    duration   {definition.duration}")
    console.warn(f"    bandwidth  {definition.target_bandwidth}")
    console.warn("")
    console.warn(f"Run id     {submission.run_id}")
    console.warn(f"Namespace  {submission.namespace}")
    console.warn(f"Image      {submission.image}")
    console.warn(f"Deadline   {deadline_seconds(definition)}s")
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
