# Makefile for CoderAI

.PHONY: help install dev test clean run lint format setup

help:
	@echo "CoderAI Development Commands"
	@echo "============================"
	@echo "make install    - Install the package"
	@echo "make dev        - Install in development mode"
	@echo "make test       - Run test suite"
	@echo "make clean      - Clean build artifacts"
	@echo "make run        - Run interactive chat"
	@echo "make lint       - Run linter"
	@echo "make format     - Format code with black"
	@echo "make setup      - Run setup wizard"

install:
	pip install -r requirements.txt
	pip install .

dev:
	pip install -r requirements.txt
	pip install -e .

test:
	pytest
	@echo ""
	@echo "Running installation smoke test..."
	python test_installation.py
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
	ruff check coderAI/ || true
	@echo ""
	@echo "Running mypy..."
	mypy coderAI/ || true

format:
	black coderAI/
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
	pip install pytest pytest-asyncio black ruff mypy
	pip install -e .

