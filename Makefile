#######################################################
# castiron — developer tasks (uv + hatchling)         #
#######################################################

.PHONY: help sync format lint typecheck vulture test test-unit coverage \
        validate build check-next-version serve-docs pre-commit-setup clean

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

test: ## Run all tests with coverage (90% floor)
	@uv run pytest -vv --cov=src/castiron --cov-report=term-missing --cov-fail-under=90

test-unit: ## Run only unit tests
	@uv run pytest -vv -m unit tests/unit/

coverage: ## Generate an HTML coverage report
	@uv run pytest --cov=src/castiron --cov-report=term-missing --cov-report=html

validate: lint typecheck test ## The pre-push gate: lint + typecheck + test

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
