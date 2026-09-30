# Makefile for CoderAI

.PHONY: help install dev test test-e2e clean run lint format format-check typecheck quickstart dist check verify-dist \
	build-sha check-deps telemetry-debug e2e test-sdk test-jev audit

PYTHON ?= .venv/bin/python

help:
	@echo "CoderAI Development Commands"
	@echo "============================"
	@echo "make install       - Install the package"
	@echo "make dev           - Install in development mode"
	@echo "make test          - Run main, SDK and wire suites + CLI smoke"
	@echo "make test-e2e      - Run async wire protocol E2E suite (tests_e2e/)"
	@echo "make e2e            - Alias for test-e2e"
	@echo "make clean         - Clean build artifacts"
	@echo "make run           - Run the interactive CLI"
	@echo "make lint          - Run ruff (required for CI)"
	@echo "make typecheck     - Run mypy (required for CI)"
	@echo "make format        - Format code with ruff"
	@echo "make check         - Check source, SDK, wire, types, tests and dependency audit"
	@echo "make build-sha     - Stamp git SHA into coderai/_build_info.py"
	@echo "make check-deps    - Verify installed deps match pyproject.toml"
	@echo "make telemetry-debug - Run local telemetry inspector (http://127.0.0.1:8765)"

install:
	$(PYTHON) -m pip install .

dev:
	$(PYTHON) -m pip install -e ".[dev,jev]" -e sdks/coderai-sdk

test:
	$(PYTHON) scripts/verification.py test --suite all

test-e2e:
	$(PYTHON) scripts/verification.py test --suite wire

test-sdk:
	$(PYTHON) scripts/verification.py test --suite sdk

test-jev:
	$(PYTHON) scripts/verification.py test --suite jev

audit:
	$(PYTHON) scripts/verification.py audit

e2e: test-e2e

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
	$(PYTHON) scripts/verification.py lint

typecheck:
	$(PYTHON) scripts/verification.py typecheck

format:
	$(PYTHON) scripts/verification.py format

format-check:
	$(PYTHON) scripts/verification.py format-check

check:
	$(PYTHON) scripts/verification.py check --report .verification/tests.json

# Quick start for new developers
quickstart: clean dev check-deps test
	@echo ""
	@echo "✓ CoderAI is ready!"

# Build distribution (requires: pip install build)
dist: clean build-sha
	$(PYTHON) -m build
	@echo "Distribution built in dist/"

verify-dist: dist
	$(PYTHON) scripts/verify_wheel.py dist

# --- Phase 6: release automation & developer tooling --------------------

# Stamp git commit SHA + origin remote into coderai/_build_info.py so
# telemetry can distinguish official builds from forks/dirty installs.
build-sha:
	$(PYTHON) scripts/inject_build_sha.py

# Verify installed runtime deps satisfy pyproject.toml constraints.
check-deps:
	$(PYTHON) scripts/verification.py deps

# Local telemetry inspector: point the transport endpoint here and watch
# outbound events. See scripts/telemetry_debug_server.py --help.
telemetry-debug:
	$(PYTHON) scripts/telemetry_debug_server.py
