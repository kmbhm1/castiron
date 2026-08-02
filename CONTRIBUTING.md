# Contributing to castiron

Thanks for your interest! castiron is pre-alpha and moving fast, but the ground
rules are stable.

## Toolchain

castiron uses [`uv`](https://docs.astral.sh/uv/) + `hatchling`.

```bash
uv sync                    # environment + dev dependencies
uv run pytest              # tests
make validate              # ruff + mypy + pytest — run before every push
make help                  # list all targets
```

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
  `BREAKING CHANGE:` footer → major. Releases are cut automatically by
  `python-semantic-release` on merge to `main` — **never hand-edit the version in
  `pyproject.toml` or `CHANGELOG.md`.**
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
