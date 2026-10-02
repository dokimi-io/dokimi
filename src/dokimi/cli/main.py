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
from dokimi.cli.commands import (
    Console,
    attach,
    convert,
    execute,
    show_image,
    submit_run,
    validate,
)
from dokimi.cli.exit_codes import ExitCode
from dokimi.execution import RobotRunError
from dokimi.images import ImageResolutionError
from dokimi.k8s.cluster import ClusterError
from dokimi.k8s.exclusivity import RunExclusivityError
from dokimi.k8s.ownership import OwnershipError
from dokimi.k8s.submission import RunNotFound
from dokimi.model import ConfigError
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
    _add_cluster_arguments(submit_parser)
    _add_image_argument(submit_parser)

    attach_parser = commands.add_parser(
        "attach",
        help="reattach to a running run and resume streaming",
    )
    attach_parser.add_argument("run_id", metavar="run-id", help="the run to follow")
    _add_cluster_arguments(attach_parser)

    image_parser = commands.add_parser(
        "image",
        help="show the images a run would submit and deploy",
    )
    _add_image_argument(image_parser)

    # Not for people: this is what the submitted Job runs, and the CLI/Job contract is
    # one decision (ADR-01-06) rather than two programs that must agree by convention.
    # Passing no `help` is what keeps it out of the command list; `help=SUPPRESS` would
    # list it with "==SUPPRESS==" as its description.
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--definition", type=Path, required=True)
    execute_parser.add_argument("--run-id", required=True)
    execute_parser.add_argument("--namespace", required=True)
    execute_parser.add_argument("--deadline-seconds", type=int, required=True)

    return parser


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
    except RunNotFound as error:
        console.fail(str(error))
        return ExitCode.CONFIG_ERROR
    except (RunExclusivityError, OwnershipError) as error:
        # ADR-01-11: another run holds the cluster, or an owner belongs to another run.
        # A run-level precondition checked before any workload exists, so a preflight
        # failure (4). A refusal's message names its `run_already_active` reason.
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
            service_account=args.service_account,
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
        )
    if args.command == "image":
        return show_image(console, image_override=args.image_override, images=args.images)
    if args.command == "execute":
        return execute(
            console,
            path=args.definition,
            run_id=args.run_id,
            namespace=args.namespace,
            deadline_seconds=args.deadline_seconds,
        )
    raise AssertionError(f"unreachable: argparse accepted {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
