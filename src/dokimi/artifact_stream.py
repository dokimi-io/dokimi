"""A run's artifacts, carried back to the submitter in the Job's own log.

**Provisional:** [ADR-05-15] is still proposed. It recommends this transport, and this
module implements that recommendation until the ADR is decided; if the decision lands
elsewhere, this is the module that changes.

The Job's filesystem disappears with its pod, and a completed pod cannot be exec'd
into. What the submitter can always read, for as long as the pod exists, is its log:
``submit`` streams it, and ``attach`` streams it again from anywhere. So at the end of a
run, the Job writes its artifact directory into that log as one block:

* a gzip-compressed tar of the directory, base64-encoded in short lines;
* between a begin and an end marker, which carry the ``run_id``, the archive's size and
  its SHA-256.

The CLI takes the block out of what it prints, checks the size and the checksum, and
extracts it with ``tarfile``'s ``data`` filter: no absolute paths, no ``..``, nothing
that points outside the destination. A block that fails any check is reported and
nothing is extracted. **The log is the transport, not the record:** the extracted
directory is the artifact.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import tarfile
from collections.abc import Callable
from pathlib import Path

__all__ = ["ArtifactReceiver", "ArtifactStreamError", "write_artifacts"]

_LINE_LENGTH = 76
_BEGIN = re.compile(
    r"^::dokimi-artifacts begin run=(?P<run>\S+) bytes=(?P<size>\d+)"
    r" sha256=(?P<digest>[0-9a-f]{64})::$"
)
_END = re.compile(r"^::dokimi-artifacts end run=(?P<run>\S+)::$")


class ArtifactStreamError(RuntimeError):
    """A block that cannot be trusted: truncated, altered, or unsafe to extract."""


def write_artifacts(directory: Path, run_id: str, write: Callable[[str], None]) -> int:
    """Write *directory* into the log through *write*, one line per call.

    Returns the archive's size in bytes. Members are stored relative to *directory*, so
    extracting it reproduces the directory's contents and nothing above it.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    data = buffer.getvalue()
    encoded = base64.b64encode(data).decode("ascii")
    write(
        f"::dokimi-artifacts begin run={run_id} bytes={len(data)} "
        f"sha256={hashlib.sha256(data).hexdigest()}::"
    )
    for start in range(0, len(encoded), _LINE_LENGTH):
        write(encoded[start : start + _LINE_LENGTH])
    write(f"::dokimi-artifacts end run={run_id}::")
    return len(data)


class ArtifactReceiver:
    """Takes a run's artifact block out of its streamed log, and keeps it.

    Feed every line through :meth:`feed`. It returns the line to print, or ``None`` for a
    line that belongs to the block. A block for another run is not this run's, and is
    passed through as ordinary log lines.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._lines: list[str] | None = None
        self._expected: tuple[int, str] | None = None
        self._archive: bytes | None = None
        self.error: ArtifactStreamError | None = None

    @property
    def received(self) -> bool:
        """Whether a complete, verified block arrived."""
        return self._archive is not None

    @property
    def incomplete(self) -> bool:
        """Whether a block began and never ended: the stream stopped inside it."""
        return self._lines is not None

    def feed(self, line: str) -> str | None:
        stripped = line.strip()
        if self._lines is None:
            begin = _BEGIN.match(stripped)
            if begin is None or begin["run"] != self.run_id:
                return line
            self._lines = []
            self._expected = (int(begin["size"]), begin["digest"])
            return None
        end = _END.match(stripped)
        if end is None:
            self._lines.append(stripped)
            return None
        self._finish("".join(self._lines))
        self._lines = None
        return None

    def extract(self, destination: Path) -> Path:
        """Extract the verified block into *destination*, which is created.

        Raises:
            ArtifactStreamError: if no verified block arrived, or the archive holds a
                member that would land outside *destination*.
        """
        if self._archive is None:
            raise self.error or ArtifactStreamError("the run's log carried no artifacts")
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(self._archive), mode="r:gz") as archive:
            try:
                archive.extractall(destination, filter="data")
            except tarfile.FilterError as error:
                raise ArtifactStreamError(f"refusing to extract the artifacts: {error}") from None
        return destination

    def _finish(self, encoded: str) -> None:
        if self._expected is None:
            return
        size, digest = self._expected
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            self.error = ArtifactStreamError("the artifact block is not valid base64")
            return
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            self.error = ArtifactStreamError(
                f"the artifact block is {len(data)} bytes with a different checksum than the "
                f"{size} bytes announced, so it was truncated or altered on the way"
            )
            return
        self._archive = data
