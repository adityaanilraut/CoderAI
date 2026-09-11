#!/usr/bin/env bash
# CoderAI CLI Installation Script
# Usage: curl -fsSL https://raw.githubusercontent.com/.../scripts/install.sh | bash

set -euo pipefail

PACKAGE_NAME="coderai-agent"
MIN_PYTHON_VERSION="3.10"

echo "=== CoderAI CLI Installer ==="

# Check Python availability
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
else
    echo "Error: Python 3 (>= ${MIN_PYTHON_VERSION}) is required but not found in PATH." >&2
    exit 1
fi

# Check Python version
PY_VER=$($PYTHON_CMD -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$($PYTHON_CMD -c 'import sys; print(int(sys.version_info >= (3, 10)))')

if [ "$PY_OK" -ne 1 ]; then
    echo "Error: Python >= ${MIN_PYTHON_VERSION} is required. Found Python ${PY_VER}." >&2
    exit 1
fi

echo "Found Python ${PY_VER} (${PYTHON_CMD})"

# Determine installer: prefer uv or pipx if available, fallback to pip
if command -v uv >/dev/null 2>&1; then
    echo "Using uv to install ${PACKAGE_NAME}..."
    uv tool install "${PACKAGE_NAME}"
elif command -v pipx >/dev/null 2>&1; then
    echo "Using pipx to install ${PACKAGE_NAME}..."
    pipx install "${PACKAGE_NAME}"
else
    echo "Using pip to install ${PACKAGE_NAME}..."
    $PYTHON_CMD -m pip install --user "${PACKAGE_NAME}"
fi

echo ""
echo "✓ Installation complete!"
echo "Run 'coderai --help' or 'cai --help' to get started."
