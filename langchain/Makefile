.PHONY: help install test integration_test lint format typecheck clean

help:
	@echo "Targets:"
	@echo "  install           Sync dev + test dependencies"
	@echo "  test              Run unit tests (no network)"
	@echo "  integration_test  Run integration tests (requires real DOCX fixtures)"
	@echo "  lint              Run ruff in check mode"
	@echo "  format            Apply ruff formatter and import sorter"
	@echo "  typecheck         Run mypy"
	@echo "  clean             Remove build artifacts and caches"

install:
	uv sync --group dev --group test

test:
	uv run --group test pytest --disable-socket --allow-unix-socket tests/unit_tests/ -v

integration_test:
	uv run --group test pytest tests/integration_tests/ -v

lint:
	uv run --group dev ruff check langchain_adeu tests

format:
	uv run --group dev ruff format langchain_adeu tests
	uv run --group dev ruff check --fix langchain_adeu tests

typecheck:
	uv run --group dev mypy langchain_adeu

clean:
	rm -rf dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +