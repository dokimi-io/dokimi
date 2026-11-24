# Examples

Robot and YAML are peer authoring formats. Neither is consumed directly: both `parse()`
into one canonical, in-memory test definition model, and the orchestration library
consumes only that model.

The two files here are the **same test** in both formats — not two hand-written documents
that happen to agree. Both are rendered from one model, and `tests/test_examples.py`
parses them on every run and asserts they still describe the same thing. A change to
either format that broke the other would fail there rather than in a reader's editor.

```console
$ uv run python -c "
from pathlib import Path
from dokimi.formats import parse_file
print(parse_file(Path('examples/worker-01-to-worker-02-udp.yaml')))"
```
