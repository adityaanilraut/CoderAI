"""Shared identity sentinels and markers for execution-loop phases."""

PROCEED_TO_TOOLS = object()
RESTART_ITERATION = object()
CANCELLED_REQUEST = object()
RECOVERABLE_ERROR_MARKER = "[Recoverable Error]:"
