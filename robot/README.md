# Robot Framework adapter

The Robot-facing surface of the project, kept outside `src/` on purpose. Files here may
know Robot Framework exists; the orchestration library in `src/test_orchestrator/` may
not (ADR-01-02, ADR-01-08).

| Path | Contents |
| --- | --- |
| `resources/` | Robot resource files a user-facing suite imports |
| `libraries/` | Thin adapter exposing the orchestration library as Robot keywords |

The keyword vocabulary arrives with M1-11 (#46); both files here are scaffolding.

## A gotcha worth knowing

This directory is named `robot`, which is also Robot Framework's own top-level package
name. When Robot Framework **is** installed, it wins: a regular package beats a PEP 420
namespace portion, so `import robot` and `import robot.api` resolve to the real library
even when the current directory is the repository root.

When Robot Framework is **not** installed, `import robot` still succeeds from the
repository root — it resolves to this directory as an empty namespace package. That makes
`import robot` useless as a check for "is Robot Framework installed", which is why CI's
`core-without-robot` job asks `importlib.metadata` for the `robotframework`
*distribution* instead.
