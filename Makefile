# Makefile for CoderAI

.PHONY: help install dev test test-security clean run lint format typecheck setup install-dev quickstart dist check lock audit

help:
	@echo "CoderAI Development Commands"
	@echo "============================"
	@echo "make install       - Install the package"
	@echo "make dev           - Install in development mode (alias: install-dev)"
	@echo "make test          - Run test suite"
	@echo "make test-security - Run only the red-team security suite (pytest -m security)"
	@echo "make clean         - Clean build artifacts"
	@echo "make run           - Run 'coderAI chat' (Textual TUI)"
	@echo "make lint          - Run ruff (required for CI)"
	@echo "make typecheck     - Run mypy (required for CI; strict per-module)"
	@echo "make format        - Format code with ruff"
	@echo "make check         - Run format, lint, typecheck, and test"
	@echo "make lock          - Regenerate the pinned, hashed requirements.lock (uv)"
	@echo "make audit         - Audit locked dependencies for known CVEs (pip-audit)"
	@echo "make setup         - Run setup wizard"

install:
	pip install .

dev:
	pip install -e ".[dev]"

test:
	pytest
	@echo ""
	@echo "Running basic CLI smoke test..."
	coderAI --version

test-security:
	pytest -m security -q

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf __pycache__/
	rm -rf coderAI/__pycache__/
	rm -rf coderAI/**/__pycache__/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	rm -rf .benchmarks/
	rm -f .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

run:
	coderAI chat

lint:
	@echo "Running ruff..."
	python3 -m ruff check coderAI/

typecheck:
	@echo "Running mypy..."
	python3 -m mypy coderAI/

format:
	python3 -m ruff format coderAI/
	@echo "Code formatted with ruff"

check: format lint typecheck test

# Regenerate the pinned, hashed lockfile from pyproject.toml (single source of
# truth). Universal resolution so one file covers Linux/macOS/Windows + py3.10+.
lock:
	uv pip compile pyproject.toml --universal --generate-hashes -o requirements.lock

# Audit the locked dependency set against the PyPI advisory database.
audit:
	pip-audit -r requirements.lock --desc --strict

setup:
	coderAI setup

# Quick start for new developers
quickstart: clean dev test
	@echo ""
	@echo "✓ CoderAI is ready!"
	@echo ""
	@echo "Run 'make setup' to configure, then 'make run' to start."

# Build distribution (requires: pip install build)
dist: clean
	python3 -m build
	@echo "Distribution built in dist/"

# Alias for `make dev`
install-dev: dev

