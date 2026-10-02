# Robot Framework adapter

The Robot-facing surface of the project, kept outside `src/` on purpose. Files here may
know Robot Framework exists; the orchestration library in `src/dokimi/` may
not (ADR-01-02, ADR-01-08).

| Path | Contents |
| --- | --- |
| `resources/` | Robot resource files a user-facing suite imports |
| `libraries/` | Thin adapter exposing the orchestration library as Robot keywords |

The orchestrator image ships both under `/opt/dokimi/robot` (`DOKIMI_ROBOT_DIR`), and phase 6
starts every pair's Robot process with that directory on its `--pythonpath`, which is how the
rendered suite's `Resource resources/Dokimi.resource` resolves.

## The keywords

`DokimiLibrary` is a dynamic library, and all it does is translate arguments and call
`dokimi`:

| Keyword | What it calls |
| --- | --- |
| `Run Network Test` | `dokimi.execution.run_network_test`: deploy the pair, pass the readiness gate, start, and wait for completion |
| The criterion keywords, such as `Throughput Should Be` | `dokimi.validation.evaluate`. Evaluating criteria arrives with M1-13: until then each one **fails**, naming the criterion it could not check, and continues, so that every criterion appears in `log.html` |

The criterion keywords come from the same table the Robot format's parser reads
(`criterion_keyword_names()`), so a suite the parser accepts never calls a keyword this library
lacks. Each Robot process learns its run from the environment phase 6 starts it with:
`DOKIMI_RUN_ID`, `DOKIMI_NAMESPACE`, `DOKIMI_TEST_ID` and the image variables.

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
