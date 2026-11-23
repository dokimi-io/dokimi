"""A run's artifacts, carried back to the submitter in the Job's own log.

The Job's filesystem disappears with its pod, and a completed pod cannot be exec'd
into. What the submitter can always read, for as long as the pod exists, is its log:
``submit`` streams it, and ``attach`` streams it again from anywhere. So at the end of a
run, the Job writes its artifact directory into that log as one block:

* a gzip-compressed tar of the directory, base64-encoded in short lines;
* between a begin and an end marker, which carry the framing version
  (:data:`FORMAT_VERSION`), the ``run_id``, the archive's size and its SHA-256. The image
  and the CLI can be of different versions, so the framing is an interface between them,
  and a version this CLI does not read is reported rather than guessed at.

The CLI takes the block out of what it prints, checks the size and the checksum, and
checks every member against ``tarfile``'s ``data`` filter before it writes any of them:
no absolute paths, no ``..``, nothing that points outside the destination. A block that
fails any check is reported and nothing is extracted. **The log is the transport, not the
record:** the extracted directory is the artifact.

Two things share the block's log. The container runtime reads the Job's stdout and
stderr separately, so a line written to stderr before or after the block can arrive in
the middle of it, whatever the process flushed; on kind, a note written just after the
block landed 2,281 lines into it. And the log is also Robot's console output, so a line
that only looks like a begin marker can come first. So a line inside a block that is not
base64 is printed as it arrives and the block goes on, and a later begin marker starts
over, printing the lines taken so far after all. The last complete block is the one kept.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import tarfile
import zlib
from collections.abc import Callable
from pathlib import Path

__all__ = ["FORMAT_VERSION", "ArtifactReceiver", "ArtifactStreamError", "write_artifacts"]

#: The framing this module writes and reads. Raised when the framing changes.
FORMAT_VERSION = 1

_LINE_LENGTH = 76
_BEGIN = re.compile(
    r"^::dokimi-artifacts begin v=(?P<version>\d+) run=(?P<run>\S+) bytes=(?P<size>\d+)"
    r" sha256=(?P<digest>[0-9a-f]{64})::$"
)
_END = re.compile(r"^::dokimi-artifacts end run=(?P<run>\S+)::$")
_DATA = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


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
        f"::dokimi-artifacts begin v={FORMAT_VERSION} run={run_id} bytes={len(data)} "
        f"sha256={hashlib.sha256(data).hexdigest()}::"
    )
    for start in range(0, len(encoded), _LINE_LENGTH):
        write(encoded[start : start + _LINE_LENGTH])
    write(f"::dokimi-artifacts end run={run_id}::")
    return len(data)


class ArtifactReceiver:
    """Takes a run's artifact block out of its streamed log, and keeps it.

    Feed every line through :meth:`feed`. It returns the lines to print: none for a line
    that belongs to the block, the line itself for one that does not, even inside a block,
    and the lines it had taken when a later begin marker shows that what looked like a
    block was not one. A block for another run is not this run's, and is passed through
    as ordinary log lines.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        #: The raw lines of the block being read, begin marker first; ``None`` outside one.
        self._raw: list[str] | None = None
        self._expected: tuple[int, int, str] | None = None
        self._archive: bytes | None = None
        self.error: ArtifactStreamError | None = None

    @property
    def received(self) -> bool:
        """Whether a complete, verified block arrived."""
        return self._archive is not None

    @property
    def incomplete(self) -> bool:
        """Whether a block began and never ended: the stream stopped inside it."""
        return self._raw is not None

    def feed(self, line: str) -> list[str]:
        stripped = line.strip()
        begin = _BEGIN.match(stripped)
        if begin is not None and begin["run"] != self.run_id:
            begin = None
        if self._raw is None:
            if begin is None:
                return [line]
            self._start(line, begin)
            return []
        if begin is not None:
            # Another begin: what came before was not this block. The last one counts.
            taken = self._raw
            self._start(line, begin)
            return taken
        end = _END.match(stripped)
        if end is not None and end["run"] == self.run_id:
            raw, self._raw = self._raw, None
            self._finish("".join(entry.strip() for entry in raw[1:]))
            return []
        if not _DATA.match(stripped):
            # Not part of the block: a line from the other stream, interleaved by the
            # container runtime, or ordinary log after a begin that was not one. Printed
            # as it arrives; the block goes on, and its checksum still decides.
            return [line]
        self._raw.append(line)
        return []

    def extract(self, destination: Path) -> Path:
        """Extract the verified block into *destination*, which is created.

        Raises:
            ArtifactStreamError: if no verified block arrived, or the archive holds a
                member that would land outside *destination*.
        """
        if self._archive is None:
            raise self.error or ArtifactStreamError("the run's log carried no artifacts")
        try:
            with tarfile.open(fileobj=io.BytesIO(self._archive), mode="r:gz") as archive:
                # Every member is checked before any is written: extractall() filters one
                # at a time, and would leave the members before a refused one behind.
                members = archive.getmembers()
                for member in members:
                    tarfile.data_filter(member, str(destination))
                destination.mkdir(parents=True, exist_ok=True)
                archive.extractall(destination, members=members, filter="data")
        except tarfile.FilterError as error:
            raise ArtifactStreamError(f"refusing to extract the artifacts: {error}") from None
        except (tarfile.TarError, EOFError, zlib.error) as error:
            # The checksum matched, so the block arrived as written: the archive itself
            # is damaged.
            raise ArtifactStreamError(f"the artifact archive cannot be read: {error}") from None
        return destination

    def _start(self, line: str, begin: re.Match[str]) -> None:
        self._raw = [line]
        self._expected = (int(begin["version"]), int(begin["size"]), begin["digest"])

    def _finish(self, encoded: str) -> None:
        if self._expected is None:
            return
        version, size, digest = self._expected
        if version != FORMAT_VERSION:
            self.error = ArtifactStreamError(
                f"the artifact block uses framing version {version}, and this dokimi reads "
                f"version {FORMAT_VERSION}. Use a dokimi of the same version as the run's image"
            )
            return
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
        self._archive, self.error = data, None
