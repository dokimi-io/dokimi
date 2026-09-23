"""CLI entry point.

Scaffolding only. Argument parsing, validation, dry-run rendering and Job submission
arrive with M1-04 (issue #39); the run's exit-code contract arrives with M1-16
(issue #51).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from test_orchestrator import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"--version", "-V"}:
        print(f"test-orchestrator {__version__}")
        return 0
    print(f"test-orchestrator {__version__} (scaffolding — no commands implemented yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
