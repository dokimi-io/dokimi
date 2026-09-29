# Contributing to dokimi

Thank you for considering it. dokimi is young: the first release, 0.1, is still being built, and
things move quickly. This page says how we work, and what we hold every change to, including our
own.

## Before you start

- **A small fix** (a typo, a wrong message, a missing test) can go straight to a pull request.
- **Anything larger, open an issue first.** Say what you want to change and why. It is cheaper to
  find out early that something is planned differently than to rework a finished pull request.
- **A change to behaviour a user can see needs a decision first.** That includes a new field, a
  changed default, a new exit code, a new permission, or a change to what a run creates or
  measures. See [How decisions are made](#how-decisions-are-made).

[`docs/setup.md`](docs/setup.md) sets up a development environment in one command, and the
[test suite README](tests/README.md) explains how the tests work without a cluster.

## How decisions are made

Every architectural decision is recorded as an architecture decision record (ADR): the context,
the options considered, the decision and its consequences, and the people who made it. The
maintainers keep these records. Code cites the decision it implements, as in `ADR-05-13`, so that
a reader can tell a deliberate choice from an accident.

- **Code follows the recorded decisions.** If your change needs a different decision, raise it in
  the issue. The maintainers record the outcome before the code changes.
- **A change that deviates from a decision says so**, in the pull request and in the code, rather
  than quietly doing something else.
- **An open question stays open until it is decided.** Code does not settle it by picking an answer
  and moving on. If you have to choose for now, say so, and say what would change the choice.

## What we hold every change to

### Clean code

- **Readable first.** Names say what a thing is or does. A function does one thing. Prefer plain
  code to clever code, and delete what nothing uses.
- **Comments explain why, not what.** The code says what it does. A comment is for the reason
  behind it, the decision it follows, or the trap it avoids. Every module starts with a docstring
  that says what it is for.
- **Match the code around you**, in naming, structure and comment density.
- **Typed throughout.** `mypy --strict` passes, and so do `ruff check` and `ruff format`, with a
  line length of 100.
- **The core library does not import Robot Framework.** Robot is an optional extra, and the core
  stays buildable, callable and testable without it.

### Behaviour we don't compromise on

These run through the whole code base. A change that weakens one of them needs a decision, not
just a review.

- **Strict input.** Unknown fields, keywords and values are refused, never ignored. A silently
  ignored typo is a test that ran with the wrong settings and reported success.
- **Errors name the fix.** A message says what is wrong, where, and what to do about it.
- **Never report success for something that didn't happen.** A run that stops early never exits
  `0`. Missing data is an error, never a pass. An error is kept apart from a failure.
- **Touch only what a run created.** Every object is labelled with its run, every lookup and
  delete is scoped by those labels, and never by name alone.
- **Least privilege.** Every permission in `deploy/rbac.yaml` is commented with the phase that
  needs it, and a test keeps the file in step with the code.
- **Pinned, not floating.** GitHub Actions are pinned to a commit, container images to a digest,
  and dependencies through `uv.lock`.

### Tests

- **Every change comes with tests**, and the four checks CI runs pass locally:

  ```console
  $ uv run ruff check .
  $ uv run ruff format --check .
  $ uv run mypy
  $ uv run pytest
  ```

- **No cluster, network or container runtime in the test suite.** Use the fakes in `tests/`. Code
  that must not reach a cluster at all is tested against a fake that raises on contact, so that
  the test passing *is* the proof.
- **Test names are sentences** that state the behaviour:
  `test_a_definition_round_trips_through_the_rendered_suite`, not `test_formats_2`.
- **Test decisions, not only behaviour.** Where a promise could break while the code keeps working,
  such as a Dockerfile that changes its base image or an RBAC file that drifts from the code, a test
  reads it and checks the promise.
- **Say whether a change ran on a real cluster.** If it touches what happens in a cluster, try it on
  the [test environment](test-env/README.md) and name the profile in the pull request. If you
  couldn't, say that too.

### Documentation

- **Documentation changes with the behaviour, in the same pull request.** That includes the README,
  [`docs/`](docs/README.md), and the tables and counts in the [test suite README](tests/README.md).
- **Describe only what exists.** Anything planned is marked as planned. A reader must be able to
  trust that what the documentation says works today really does.
- **Every example parses.** Test definitions in the documentation are checked by the test suite.
- **Write plainly.** Short sentences, concrete examples, no marketing.

## Pull requests

- **One topic per pull request.** A refactoring that a feature needs goes first, on its own.
- **The title is what the change does**, as it should read in the history: `Refuse an unknown
  monitoring mode before submitting`. A pull request for a planned work item starts with its id,
  for example `M1-08: managed Prometheus provisioning and query access`.
- **The description is the record.** Say:
  - what changed, and what was decided, with the reasoning;
  - what was deliberately **not** done, and why. A reader can't tell an intentional omission from an
    oversight unless it is written down;
  - for a work item, the state of each acceptance criterion, met or not;
  - what is left as follow-up, and where it will be done.
- **Tell the reviewer where to look.** Add a separate comment headed *What the reviewer should
  specifically check*. Use it for judgment calls that could have gone the other way, costs you
  accepted, and claims about decisions that someone should verify. It names what could be
  **wrong**; it is not a second summary.
- **Every review point gets an answer:** fixed, or moved to a named follow-up that is noted on its
  issue, so that nothing is lost.
- **A pull request merges when CI is green and a maintainer has approved it.** It is squash-merged,
  so its title becomes the commit.

## AI assistance

dokimi is built with AI coding assistants, currently Claude Code. We use them to draft code, tests,
documentation and design documents. We don't use them to decide.

- **Decisions are ours.** Every architectural decision is recorded with the people who made it. An
  assistant can lay out the options; choosing between them is our job.
- **Every change is reviewed by one of us before it merges**, and changed where we think it's wrong.
  Code we don't understand doesn't merge.
- **It is visible.** Commits written with an assistant carry a `Co-Authored-By:` trailer naming it,
  and pull requests drafted with one say so.

The same applies to contributions. You are welcome to use AI assistance, but you are the author:
say so in the pull request, and be ready to explain every line of it. The standards on this page
apply however the change was written.

## License

dokimi is released under the [MIT License](LICENSE). By contributing, you agree that your
contribution is released under the same license.
