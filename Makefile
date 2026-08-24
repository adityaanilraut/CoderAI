# Makefile for CoderAI

.PHONY: help install dev test clean run lint format format-check typecheck quickstart dist check verify-dist

PYTHON ?= python3

help:
	@echo "CoderAI Development Commands"
	@echo "============================"
	@echo "make install       - Install the package"
	@echo "make dev           - Install in development mode"
	@echo "make test          - Run test suite + CLI smoke test"
	@echo "make clean         - Clean build artifacts"
	@echo "make run           - Run the interactive CLI"
	@echo "make lint          - Run ruff (required for CI)"
	@echo "make typecheck     - Run mypy (required for CI)"
	@echo "make format        - Format code with ruff"
	@echo "make check         - Check format, lint, types, and tests without modifying files"

install:
	$(PYTHON) -m pip install .

dev:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest
	@echo ""
	@echo "Running basic CLI smoke test..."
	$(PYTHON) -m coderai --version

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
	$(PYTHON) -m coderai

lint:
	@echo "Running ruff..."
	$(PYTHON) -m ruff check coderai/ tests/ scripts/

typecheck:
	@echo "Running mypy..."
	$(PYTHON) -m mypy coderai/

format:
	$(PYTHON) -m ruff format coderai/ tests/ scripts/
	@echo "Code formatted with ruff"

format-check:
	$(PYTHON) -m ruff format --check coderai/ tests/ scripts/

check: format-check lint typecheck test

# Quick start for new developers
quickstart: clean dev test
	@echo ""
	@echo "✓ CoderAI is ready!"

# Build distribution (requires: pip install build)
dist: clean
	$(PYTHON) -m build
	@echo "Distribution built in dist/"

verify-dist: dist
	$(PYTHON) scripts/verify_wheel.py dist
