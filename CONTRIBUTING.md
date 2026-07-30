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
docs/              MkDocs Material site
```
