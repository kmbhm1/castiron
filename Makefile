#######################################################
# castiron — developer tasks (uv + hatchling)         #
#######################################################

.PHONY: help sync format lint typecheck typecheck-matrix vulture test test-unit \
        test-integration test-matrix coverage validate validate-fast build \
        check-next-version serve-docs pre-commit-setup clean

help: ## Display this help message
	@echo "castiron — available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

sync: ## Create the environment and install dev dependencies
	@uv sync

format: ## Run the ruff formatter
	@uv run ruff format .

lint: ## Sort imports + lint with ruff
	@uv run ruff check --select I,UP007,F401,UP006 --fix .
	@uv run ruff check .

typecheck: ## Type-check src/ with mypy (strict)
	@uv run mypy src

vulture: ## Find unused code with vulture
	@uv run vulture src/

# `-m "not integration"` is what keeps the static gate offline: the live-source suite under
# tests/integration/ needs the external castiron-testbed apparatus, which most machines running
# this target do not have. It skips itself when the apparatus is absent, but it is excluded here
# as well so a developer who DOES have it exported still gets a network-free `make validate`.
test: ## Run all tests with coverage (90% floor); excludes the live-source suite
	@uv run pytest -vv -m "not integration" --cov=src/castiron --cov-report=term-missing --cov-fail-under=90

test-unit: ## Run only unit tests
	@uv run pytest -vv -m unit tests/unit/

test-integration: ## Run the live-source suite (needs the castiron-testbed apparatus; see tests/integration/README.md)
	@uv run pytest -vv -m integration tests/integration/

coverage: ## Generate an HTML coverage report
	@uv run pytest --cov=src/castiron --cov-report=term-missing --cov-report=html

# NOTE the interpreter ORDER: the pinned one (3.12, per .python-version) runs LAST.
# `uv run --python X` tears down and recreates .venv whenever X differs from the current one,
# so ending on any other version would leave the tree on the wrong interpreter and force a
# silent rebuild on the next plain `uv run`. Ending on 3.12 leaves it exactly as it was found.
# Coverage is enforced once, on that final 3.12 leg — the floor is a property of the suite,
# not of the interpreter, and gating it four times only multiplies runtime.
#
# ⚠ `-m "not integration"` is on BOTH legs, and it is not decoration. This target is what
# `validate` runs, so omitting it here would silently undo the exclusion `test` makes above:
# a developer with CASTIRON_TEST_POSTGREST_URL exported would get a `make validate` that opens
# sockets — falsifying, in one stroke, the promise made by this file, CONTRIBUTING.md,
# tests/integration/README.md and tests/integration/conftest.py. The autouse skip fixture would
# still protect an *unconfigured* machine, but the guarantee those four documents sell is the
# MARKER, which holds by construction rather than by absence of configuration.
test-matrix: ## Run the suite on every CI interpreter (3.10-3.13) — coverage on 3.12 only
	@set -e; for V in 3.10 3.11 3.13 3.12; do \
		printf '\n=== pytest on py%s ===\n' "$$V"; \
		if [ "$$V" = "3.12" ]; then \
			uv run --python "$$V" pytest -q -m "not integration" \
				--cov=src/castiron --cov-report=term-missing --cov-fail-under=90; \
		else \
			uv run --python "$$V" pytest -q -m "not integration"; \
		fi; \
	done

# mypy takes --python-version as a flag, so this needs no interpreter switching and no
# .venv churn. Cheap enough to keep separate from test-matrix.
typecheck-matrix: ## mypy --strict against every CI interpreter (3.10-3.13)
	@set -e; for V in 3.10 3.11 3.12 3.13; do \
		printf '=== mypy --python-version %s ===\n' "$$V"; \
		uv run mypy src --python-version "$$V"; \
	done

# The pre-push gate. It runs the FULL CI interpreter matrix (captain decision CI-082),
# because a gate that covers less than the CI following it is theatre on the axis it omits.
#
# Why: PR #10 passed a single-interpreter `make test` and shipped CI red on py3.13 —
# CPython 3.13 dedents docstrings at compile time and expands tabs to the 8-column tab stop,
# so an assertion round-tripping through `__doc__` failed there and nowhere else. Every prior
# PR had passed that axis by luck, not verification (see QUESTIONS.md `CI-081`).
#
# Note `mypy --python-version 3.13` was ALREADY clean for that change: the divergence was in
# CPython's RUNTIME behaviour, invisible to any static check. Hence test-matrix, not just
# typecheck-matrix.
validate: lint typecheck-matrix test-matrix ## The pre-push gate: lint + typecheck + test, across py3.10-3.13

validate-fast: lint typecheck test ## Single-interpreter gate (3.12) — for iterating, NOT for push

build: ## Build sdist + wheel with uv
	@uv build

check-next-version: ## Dry-run the next semantic-release version (never applies)
	@uv run semantic-release -v --noop version --print

serve-docs: ## Serve the docs site locally
	@uv run --group docs mkdocs serve

pre-commit-setup: ## Install git hooks (pre-commit, pre-push, commit-msg)
	@uv run pre-commit install --hook-type pre-commit --hook-type pre-push --hook-type commit-msg

clean: ## Remove caches and build artifacts
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .mypy_cache .pytest_cache .ruff_cache htmlcov dist build
