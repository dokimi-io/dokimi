"""CLI entry point: parse arguments, dispatch, translate failures into exit codes.

Phase 1 of a run and the only part that executes outside the cluster (ADR-01-06). The
commands themselves live in :mod:`dokimi.cli.commands`; this module is the
adapter between a command line and those functions, and the single place where an
exception becomes an exit code.

That translation is the reason the commands raise rather than return codes. ADR-05-05
classifies a bad definition as ``config_error`` — detected before any resource is
created — and ADR-01-08 numbers it 3. Having exactly one place that knows this is what
keeps the numbering from drifting per command.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from dokimi import __version__
from dokimi.cleanup import Retention
from dokimi.cli import completion
from dokimi.cli.commands import (
    Console,
    attach,
    clean_up,
    convert,
    execute,
    open_prometheus,
    preflight,
    show_image,
    submit_run,
    validate,
)
from dokimi.cli.exit_codes import ExitCode
from dokimi.cluster_name import CLUSTER_ENV_VAR
from dokimi.execution import RobotRunError
from dokimi.images import ImageResolutionError
from dokimi.k8s.cluster import ClusterError
from dokimi.k8s.exclusivity import RunExclusivityError
from dokimi.k8s.ownership import OwnershipError
from dokimi.k8s.port_forward import ForwardingNotAllowed
from dokimi.k8s.submission import RunNotFound
from dokimi.metrics.prometheus import PrometheusUnreachableError
from dokimi.model import ConfigError
from dokimi.preflight import ClusterNotReadyError
from dokimi.run_images import COMPONENTS

__all__ = ["build_parser", "main"]

_DESCRIPTION = """\
Submit and follow network validation runs in a Kubernetes cluster.

The run itself executes in the cluster as a one-shot Job. This command validates the
definition, submits that Job and streams its logs; --dry-run stops before submitting and
touches nothing.
"""


class _Parser(argparse.ArgumentParser):
    """argparse, except that a usage error is a ``config_error``.

    argparse exits 2 on a bad flag. In ADR-01-08's table 2 is ``error`` — part of the
    matrix was not validly measured — so a typo on the command line would read to CI as a
    run that errored. A malformed invocation is an invalid request detected before
    anything was created, which is exactly what 3 means. Subparsers inherit the class.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(ExitCode.CONFIG_ERROR, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="dokimi",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"dokimi {__version__}",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    validate_parser = commands.add_parser(
        "validate",
        help="check a test definition without contacting a cluster",
    )
    validate_parser.add_argument("definition", type=Path, help="a .yaml or .robot test definition")

    convert_parser = commands.add_parser(
        "convert",
        help="print a test definition in the other format, without contacting a cluster",
    )
    convert_parser.add_argument("definition", type=Path, help="a .yaml or .robot test definition")
    convert_parser.add_argument(
        "--to",
        choices=("robot", "yaml"),
        default="robot",
        help="the format to print; robot (the default) is exactly the suite a run executes",
    )

    submit_parser = commands.add_parser(
        "submit",
        help="submit a run and stream its logs",
    )
    submit_parser.add_argument("definition", type=Path, help="a .yaml or .robot test definition")
    submit_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render the planned matrix and manifests, create nothing",
    )
    submit_parser.add_argument(
        "--no-follow",
        action="store_true",
        help="submit and return immediately instead of streaming",
    )
    submit_parser.add_argument("--run-id", help="use this run id instead of generating one")
    submit_parser.add_argument(
        "--service-account",
        help="service account for the run's Job, if the cluster needs a specific one",
    )
    submit_parser.add_argument(
        "--retain",
        choices=[retention.value for retention in Retention],
        default=Retention.ON_FAILURE.value,
        help="when the run keeps its test pods and record for inspection, for dokimi cleanup "
        "to remove later: on-failure (the default), always or never",
    )
    _add_cluster_name_argument(submit_parser)
    _add_cluster_arguments(submit_parser)
    _add_image_argument(submit_parser)

    preflight_parser = commands.add_parser(
        "preflight",
        help="check that the cluster can run a definition's tests, without running them",
        description=(
            "Submit the run's preflight and only that: claim the cluster, check the "
            "planned nodes, and deploy the first pair once to see it schedule, pull its "
            "image, connect and resolve a cluster name, then remove it. Creates real pods, "
            "unlike submit --dry-run, which renders and creates nothing. Exits 0 if the "
            "cluster is ready and 4 if it is not."
        ),
    )
    preflight_parser.add_argument(
        "definition", type=Path, help="the .yaml or .robot test definition to check for"
    )
    preflight_parser.add_argument(
        "--no-follow",
        action="store_true",
        help="submit and return immediately instead of streaming",
    )
    preflight_parser.add_argument("--run-id", help="use this run id instead of generating one")
    preflight_parser.add_argument(
        "--service-account",
        help="service account for the run's Job, if the cluster needs a specific one",
    )
    _add_cluster_name_argument(preflight_parser)
    _add_cluster_arguments(preflight_parser)
    _add_image_argument(preflight_parser)

    attach_parser = commands.add_parser(
        "attach",
        help="reattach to a running run and resume streaming",
    )
    attach_parser.add_argument("run_id", metavar="run-id", help="the run to follow")
    attach_parser.add_argument(
        "--results-directory",
        type=Path,
        help="where to extract the run's artifacts, instead of its definition's",
    )
    _add_cluster_arguments(attach_parser)

    cleanup_parser = commands.add_parser(
        "cleanup",
        help="remove what a run left in the cluster, or every run older than an age",
        description=(
            "Remove everything a run created, found by its labels, without the run's own "
            "process: its test pods, its node-exporter, its owner and definition, and with "
            "them its Job. Its Prometheus stays unless --prometheus is given. A run that "
            "still holds the cluster is refused unless --force is given. Exits 0 when "
            "nothing is left, 4 when the run still holds the cluster, and 5 when something "
            "could not be removed."
        ),
    )
    which = cleanup_parser.add_mutually_exclusive_group(required=True)
    which.add_argument("run_id", metavar="run-id", nargs="?", help="the run to remove")
    which.add_argument(
        "--older-than",
        metavar="AGE",
        help="remove every run that started more than AGE ago, for example 24h",
    )
    cleanup_parser.add_argument(
        "--prometheus",
        action="store_true",
        help="remove the run's Prometheus too, and with it the history it holds",
    )
    cleanup_parser.add_argument(
        "--force",
        action="store_true",
        help="remove a run that still holds the cluster: only when its Job is gone",
    )
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be removed, and remove nothing",
    )
    _add_cluster_arguments(cleanup_parser)

    prometheus_parser = commands.add_parser(
        "prometheus",
        help="open a run's kept Prometheus through a local port",
        description=(
            "Forward a local port to the Prometheus a run kept, found by the run's labels "
            "in its namespace, and print its URL and when the run started and ended. Needs "
            "no kubectl, but `create` on `pods/portforward` in the run's namespace. "
            "Forwards until Ctrl-C or kill. Exits 3 if the run has no Prometheus any more, "
            "or if the credentials in use may not open a port-forward."
        ),
    )
    prometheus_parser.add_argument(
        "run_id", metavar="run-id", help="the run whose Prometheus to open"
    )
    prometheus_parser.add_argument(
        "--port",
        type=_local_port,
        default=0,
        help="the local port to listen on, instead of a free one",
    )
    _add_cluster_arguments(prometheus_parser)

    image_parser = commands.add_parser(
        "image",
        help="show the images a run would submit and deploy",
    )
    _add_image_argument(image_parser)

    completion_parser = commands.add_parser(
        "completion",
        help="print a shell completion script for bash, zsh or fish",
        description=(
            "Print a script that completes dokimi's commands, flags, fixed values and files "
            "in your shell. Load it in the current shell with "
            "'eval \"$(dokimi completion bash)\"', 'source <(dokimi completion zsh)' or "
            "'dokimi completion fish | source'. To load it in every new shell, save it where "
            "your shell looks for completions; the usage guide says where."
        ),
    )
    completion_parser.add_argument(
        "shell", choices=completion.SHELLS, help="the shell to complete in"
    )

    # Not for people: this is what the submitted Job runs, and the CLI/Job contract is
    # one decision (ADR-01-06) rather than two programs that must agree by convention.
    # Passing no `help` is what keeps it out of the command list; `help=SUPPRESS` would
    # list it with "==SUPPRESS==" as its description.
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--definition", type=Path, required=True)
    execute_parser.add_argument("--run-id", required=True)
    execute_parser.add_argument("--namespace", required=True)
    execute_parser.add_argument("--deadline-seconds", type=int, required=True)
    execute_parser.add_argument("--preflight-only", action="store_true")
    # A default, so that a Job rendered by an older CLI still runs.
    execute_parser.add_argument(
        "--retain",
        choices=[retention.value for retention in Retention],
        default=Retention.ON_FAILURE.value,
    )

    return parser


def _local_port(text: str) -> int:
    """``--port``: a TCP port to listen on, 1 to 65535."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number") from None
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError(f"{port} is not a port: 1 to 65535")
    return port


def _add_cluster_name_argument(parser: argparse.ArgumentParser) -> None:
    """``--cluster``, for every command that submits a run: a preflight is a run too."""
    parser.add_argument(
        "--cluster",
        dest="cluster_name",
        help=f"the name results are labelled with, overriding ${CLUSTER_ENV_VAR}; required, "
        "with no default: lowercase letters, digits and hyphens. It only names the cluster; "
        "--kubeconfig and --context choose which one the run goes to",
    )


def _add_cluster_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--namespace", "-n", help="namespace to use instead of the test type's")
    parser.add_argument("--kubeconfig", help="path to a kubeconfig file")
    parser.add_argument("--context", help="kubeconfig context to use")


def _add_image_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--orchestrator-image",
        dest="image_override",
        help="the same as --image orchestrator=REFERENCE, its earlier name",
    )
    components = ", ".join(sorted(COMPONENTS))
    variables = ", ".join(
        "$" + " or $".join((component.env_var, *component.env_aliases))
        for component in COMPONENTS.values()
    )
    parser.add_argument(
        "--image",
        dest="images",
        action="append",
        default=[],
        metavar="COMPONENT=REFERENCE",
        help=(
            f"an image the run deploys, overriding {variables} and the default; "
            f"repeatable. Components: {components}"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code (ADR-01-08's table)."""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    console = Console(out=sys.stdout, err=sys.stderr)

    if args.command is None:
        parser.print_help()
        return ExitCode.PASS

    try:
        return _dispatch(console, args)
    except (ConfigError, ImageResolutionError) as error:
        # Before anything was created, by construction: every path that can raise
        # either of these runs before the first cluster call. Having no image to submit
        # is a problem with the request, exactly like a malformed definition.
        console.fail(str(error))
        return ExitCode.CONFIG_ERROR
    except FileNotFoundError as error:
        console.fail(f"{error.filename}: no such file")
        return ExitCode.CONFIG_ERROR
    except (RunNotFound, ForwardingNotAllowed) as error:
        # Nothing the cluster got wrong: the run asked for isn't there, or the credentials
        # in use may not do what was asked of them.
        console.fail(str(error))
        return ExitCode.CONFIG_ERROR
    except (
        RunExclusivityError,
        OwnershipError,
        PrometheusUnreachableError,
        ClusterNotReadyError,
    ) as error:
        # ADR-01-11: another run holds the cluster, or an owner belongs to another run.
        # ADR-03-02: the run's Prometheus cannot be queried. ADR-05-07: a cluster check
        # failed. Each a run-level precondition checked before any test runs, so a
        # preflight failure (4). Each message names its reason: `run_already_active`,
        # or `cluster not ready (<check>)`.
        console.fail(_with_notes(error))
        return ExitCode.PREFLIGHT_FAILURE
    except (ClusterError, RobotRunError) as error:
        # Robot Framework not being able to run at all is the orchestrator's failure,
        # never a test outcome: no report from such a run can be trusted (ADR-01-08).
        console.fail(_with_notes(error))
        return ExitCode.INTERNAL
    except Exception as error:  # noqa: BLE001 - the whole point is to catch everything
        # Anything not anticipated above is a bug here or in a dependency, never a
        # verdict. Without this, Python exits 1 — `fail` in ADR-01-08's table — so CI
        # would read a crash as a test that ran and did not pass.
        traceback.print_exc(file=sys.stderr)
        console.fail(
            f"internal error ({type(error).__name__}: {error}). This is a bug; the "
            "traceback above is what to report."
        )
        return ExitCode.INTERNAL


def _with_notes(error: BaseException) -> str:
    """The message, plus any notes attached on the way up (PEP 678)."""
    return " ".join([str(error), *getattr(error, "__notes__", ())])


def _dispatch(console: Console, args: argparse.Namespace) -> int:
    if args.command == "validate":
        return validate(console, path=args.definition)
    if args.command == "convert":
        return convert(console, path=args.definition, to=args.to)
    if args.command == "submit":
        return submit_run(
            console,
            path=args.definition,
            dry_run=args.dry_run,
            namespace=args.namespace,
            run_id=args.run_id,
            image_override=args.image_override,
            images=args.images,
            cluster_name=args.cluster_name,
            service_account=args.service_account,
            kubeconfig=args.kubeconfig,
            context=args.context,
            follow_logs=not args.no_follow,
            retention=Retention(args.retain),
        )
    if args.command == "preflight":
        return preflight(
            console,
            path=args.definition,
            namespace=args.namespace,
            run_id=args.run_id,
            image_override=args.image_override,
            images=args.images,
            service_account=args.service_account,
            cluster_name=args.cluster_name,
            kubeconfig=args.kubeconfig,
            context=args.context,
            follow_logs=not args.no_follow,
        )
    if args.command == "attach":
        return attach(
            console,
            run_id=args.run_id,
            namespace=args.namespace,
            kubeconfig=args.kubeconfig,
            context=args.context,
            results_directory=args.results_directory,
        )
    if args.command == "cleanup":
        return clean_up(
            console,
            run_id=args.run_id,
            older_than=args.older_than,
            namespace=args.namespace,
            prometheus=args.prometheus,
            force=args.force,
            dry_run=args.dry_run,
            kubeconfig=args.kubeconfig,
            context=args.context,
        )
    if args.command == "prometheus":
        return open_prometheus(
            console,
            run_id=args.run_id,
            namespace=args.namespace,
            port=args.port,
            kubeconfig=args.kubeconfig,
            context=args.context,
        )
    if args.command == "image":
        return show_image(console, image_override=args.image_override, images=args.images)
    if args.command == "completion":
        console.out.write(completion.script(args.shell, build_parser()))
        return ExitCode.PASS
    if args.command == "execute":
        return execute(
            console,
            path=args.definition,
            run_id=args.run_id,
            namespace=args.namespace,
            deadline_seconds=args.deadline_seconds,
            preflight_only=args.preflight_only,
            retention=Retention(args.retain),
        )
    raise AssertionError(f"unreachable: argparse accepted {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
