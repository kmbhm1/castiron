# Contributing to castiron

Thanks for your interest! castiron is pre-alpha and moving fast, but the ground
rules are stable.

## Toolchain

castiron uses [`uv`](https://docs.astral.sh/uv/) + `hatchling`.

```bash
uv sync                    # environment + dev dependencies
make pre-commit-setup      # install the git hooks — once, right after the sync
uv run pytest              # tests
make validate              # ruff + vulture + mypy + pytest — run before every push
make help                  # list all targets
```

### The git hooks

`make pre-commit-setup` installs three [pre-commit](https://pre-commit.com/) hook types. Do it once,
straight after `uv sync`: it is what makes the house rules below enforced rather than remembered.
Skip it and the first thing that tells you a convention was broken is CI, on your open PR.

| Hook stage | What runs |
| --- | --- |
| `commit-msg` | commitizen — rejects a message that is not a [Conventional Commit](#house-rules) |
| `pre-commit` | whitespace and end-of-file fixers, `check-yaml`, `check-toml`, `detect-private-key`, a guard against committing to `main` — plus `actionlint` and `zizmor` when the commit touches `.github/workflows/` |
| `pre-push` | `ruff check`, `ruff format`, and `mypy --strict` against Python 3.10–3.13 |

Each hook runs its own pinned tool version in an environment pre-commit builds, so the first run
after installing (or after a version moves in `.pre-commit-config.yaml`) is slow and the rest are
not. To run them across the whole tree without committing anything:

```bash
uv run pre-commit run --all-files                        # everything in the commit stage
uv run pre-commit run --all-files --hook-stage pre-push  # everything in the push stage
```

The push-stage hooks and `make validate` overlap, and neither contains the other: the hooks also
check formatting, while `make validate` also runs vulture and the test matrix. Run the gate. If a
hook does fail, fix what it found — `--no-verify` is not the way past it.

### The optional live-source tests

`make test` and `make validate` never touch the network. `tests/integration/` holds an optional
suite that runs castiron against a real PostgREST serving a real Postgres schema; it skips itself
entirely unless you export `CASTIRON_TEST_POSTGREST_URL`, and `make test` excludes it either way.
The schema it needs is a separate, disposable apparatus —
[`castiron-testbed`](https://github.com/kmbhm1/castiron-testbed) — deliberately not vendored here.
If you want to run it, `make test-integration` and `tests/integration/README.md` are the entry
points. **You do not need it to contribute.**

## House rules

- **Conventional Commits.** `type(scope): summary` — `feat`, `fix`, `docs`,
  `refactor`, `test`, `chore`, `perf`, `build`, `ci`, `style`. The commit type
  drives the release: `fix:`/`perf:` → patch, `feat:` → minor, a `!` or
  `BREAKING CHANGE:` footer → major. `python-semantic-release` computes that bump and
  cuts the release — but **while castiron is pre-alpha the release workflow is gated to
  a manual `workflow_dispatch`**, so merging to `main` publishes nothing; a maintainer
  triggers the release by hand from the Actions tab. Your commit type still decides the
  version that release carries, so choose it deliberately — and **never hand-edit the
  version in `pyproject.toml` or `CHANGELOG.md`.**
- **Typing is the contract.** `mypy --strict` over `src/`; every public function
  is fully typed.
- **Style.** `ruff` with single quotes, 120 columns, Google-convention
  docstrings. Run `make format` before committing.
- **Coverage floor is 90%**, enforced in CI on the 3.10–3.13 matrix. New behavior
  ships with tests.
- **Branch, don't commit to `main`.** Open a PR; a maintainer merges.

## Project layout

```
src/castiron/      the package (compiler pipeline)
tests/unit/        unit tests (mirror the src tree)
tests/integration/ optional live-source tests (need an external apparatus; see its README)
docs/              MkDocs Material site
```
