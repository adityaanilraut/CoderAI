# Makefile for CoderAI

.PHONY: help install dev test clean run lint format format-check typecheck install-dev quickstart dist check

PYTHON ?= python3

help:
	@echo "CoderAI Development Commands"
	@echo "============================"
	@echo "make install       - Install the package"
	@echo "make dev           - Install in development mode (alias: install-dev)"
	@echo "make test          - Run test suite + CLI smoke test"
	@echo "make clean         - Clean build artifacts"
	@echo "make run           - Run the interactive CLI"
	@echo "make lint          - Run ruff (required for CI)"
	@echo "make typecheck     - Run mypy (required for CI)"
	@echo "make format        - Format code with ruff"
	@echo "make check         - Check format, lint, types, and tests without modifying files"

install:
	pip install .

dev:
	pip install -e ".[dev]"

test:
	pytest
	@echo ""
	@echo "Running basic CLI smoke test..."
	coderai --version

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf __pycache__/
	rm -rf coderai/__pycache__/
	rm -rf coderai/**/__pycache__/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	rm -rf .benchmarks/
	rm -f .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

run:
	coderai

lint:
	@echo "Running ruff..."
	python3 -m ruff check coderai/ tests/ scripts/

typecheck:
	@echo "Running mypy..."
	python3 -m mypy coderai/

format:
	python3 -m ruff format coderai/ tests/ scripts/
	@echo "Code formatted with ruff"

format-check:
	python3 -m ruff format --check coderai/ tests/ scripts/

check: format-check lint typecheck test

# Quick start for new developers
quickstart: clean dev test
	@echo ""
	@echo "✓ CoderAI is ready!"

# Build distribution (requires: pip install build)
dist: clean
	python3 -m build
	@echo "Distribution built in dist/"

# Alias for `make dev`
install-dev: dev
