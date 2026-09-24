"""CLI entry point.

Still scaffolding. Argument parsing, validation, dry-run rendering and Job submission
arrive with M1-04 (issue #39); the run's exit-code contract arrives with M1-16 (issue
#51).

The one real command here is ``image``, which resolves the orchestrator image reference
this build would submit. M1-02 (issue #37) needs the resolution and its override to be
inspectable and testable before there is a ``submit`` to use them, and M1-04 wires the
same :func:`~test_orchestrator.images.resolve_image` call into the Job it renders.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from test_orchestrator import __version__
from test_orchestrator.images import IMAGE_OVERRIDE_ENV_VAR, ImageResolutionError, resolve_image

_USAGE = f"""\
usage: test-orchestrator [--version] <command>

commands:
  image [--orchestrator-image REFERENCE]
                              show the orchestrator image a run would submit

The reference is taken from --orchestrator-image, then from ${IMAGE_OVERRIDE_ENV_VAR},
then from the digest baked in at release time.
"""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in {"--version", "-V"}:
        print(f"test-orchestrator {__version__}")
        return 0

    if args and args[0] in {"--help", "-h"}:
        print(_USAGE, end="")
        return 0

    if args and args[0] == "image":
        return _image_command(args[1:])

    if args:
        print(f"test-orchestrator: unknown command {args[0]!r}", file=sys.stderr)
        print(_USAGE, end="", file=sys.stderr)
        return 2

    print(f"test-orchestrator {__version__} (scaffolding — no run commands implemented yet)")
    return 0


def _image_command(args: Sequence[str]) -> int:
    override: str | None = None
    remaining = list(args)
    if remaining and remaining[0] == "--orchestrator-image":
        if len(remaining) < 2:
            print("test-orchestrator: --orchestrator-image needs a reference", file=sys.stderr)
            return 2
        override, remaining = remaining[1], remaining[2:]
    if remaining:
        print(f"test-orchestrator: unexpected argument {remaining[0]!r}", file=sys.stderr)
        return 2

    try:
        image = resolve_image(override)
    except ImageResolutionError as error:
        print(f"test-orchestrator: {error}", file=sys.stderr)
        return 1

    print(image.reference)
    print(f"  source: {image.source}")
    print(f"  digest: {image.digest or 'not pinned by digest'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
