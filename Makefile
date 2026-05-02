# Makefile for CoderAI

.PHONY: help install dev test clean run lint format setup ui-install ui-dev ui-build ui-compile ui-compile-all ui-typecheck ui install-dev quickstart dist

help:
	@echo "CoderAI Development Commands"
	@echo "============================"
	@echo "make install     - Install the package"
	@echo "make dev         - Install in development mode"
	@echo "make test        - Run test suite"
	@echo "make clean       - Clean build artifacts"
	@echo "make run         - Run 'coderAI chat' (launches the Ink UI)"
	@echo "make ui          - Run the Ink (TypeScript) UI in dev mode"
	@echo "make ui-install  - Install Ink UI dependencies"
	@echo "make ui-build    - Build Ink UI to dist/cli.js (needs Node)"
	@echo "make ui-compile  - Bundle Ink UI to standalone binary (needs Bun)"
	@echo "                   Optional: TARGET=bun-linux-x64 for cross-compile"
	@echo "make ui-compile-all - Cross-compile for all supported platforms"
	@echo "make lint        - Run ruff (required for CI)"
	@echo "make typecheck   - Run mypy (optional; package not fully typed yet)"
	@echo "make format      - Format code with black"
	@echo "make setup       - Run setup wizard"

install:
	pip install -r requirements.txt
	pip install .

dev:
	pip install -r requirements.txt
	pip install -e .

test:
	pytest
	@echo ""
	@echo "Running basic CLI smoke test..."
	coderAI --version

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf __pycache__/
	rm -rf coderAI/__pycache__/
	rm -rf coderAI/**/__pycache__/
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
	python3 -m black coderAI/
	@echo "Code formatted with black"

setup:
	coderAI setup

# Quick start for new developers
quickstart: clean dev test
	@echo ""
	@echo "✓ CoderAI is ready!"
	@echo ""
	@echo "Run 'make setup' to configure, then 'make run' to start."

# Build distribution
dist: clean
	python -m build
	@echo "Distribution built in dist/"

# Install dev dependencies
install-dev:
	pip install -r requirements.txt
	pip install -e ".[dev]"

# ---- Ink (TypeScript) UI targets ------------------------------------------

ui-install:
	@command -v bun >/dev/null 2>&1 || { echo "Bun is required. Install from https://bun.sh"; exit 1; }
	cd ui && bun install

ui-dev:
	cd ui && bun run src/cli.tsx

ui-build:
	@command -v bun >/dev/null 2>&1 || { echo "Bun is required"; exit 1; }
	cd ui && bun run build

ui-compile:
	@command -v bun >/dev/null 2>&1 || { echo "Bun is required"; exit 1; }
	cd ui && BUN_TARGET=$(or $(TARGET),bun) bun run compile
	@echo "Standalone binary written under ui/dist/"

ui-compile-all:
	@command -v bun >/dev/null 2>&1 || { echo "Bun is required"; exit 1; }
	cd ui && bun run compile:all
	@echo "All platform binaries written under ui/dist/"

ui-typecheck:
	cd ui && bun run typecheck

ui: ui-dev

