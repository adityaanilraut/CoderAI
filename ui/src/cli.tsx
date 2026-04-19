#!/usr/bin/env node
/**
 * Entry point for the CoderAI Ink UI.
 *
 * Usage:
 *   coderai-ui [--python=/path/to/python]
 *
 * The Python agent is spawned as a child process and the NDJSON protocol
 * defined in `PROTOCOL.md` bridges React components to it.
 */

import React from "react";
import {render} from "ink";
import {App} from "./App.js";

function parseArgs(argv: string[]) {
  const out: {python?: string; cwd?: string; help?: boolean} = {};
  for (const arg of argv.slice(2)) {
    if (arg === "--help" || arg === "-h") out.help = true;
    else if (arg.startsWith("--python=")) out.python = arg.slice("--python=".length);
    else if (arg.startsWith("--cwd=")) out.cwd = arg.slice("--cwd=".length);
  }
  return out;
}

function printHelp() {
  console.log(`CoderAI Ink UI

Usage:
  coderai-ui [options]

Options:
  --python=PATH   Python interpreter to use (default: $CODERAI_PYTHON or python3)
  --cwd=PATH      Working directory for the agent (default: current directory)
  --help, -h      Show this help

Environment:
  CODERAI_MODEL          Override the default model
  CODERAI_AUTO_APPROVE   Set to '1' to skip tool confirmation prompts
  CODERAI_LOG_LEVEL      Python log level (DEBUG, INFO, WARNING, ERROR)
`);
}

const args = parseArgs(process.argv);
if (args.help) {
  printHelp();
  process.exit(0);
}

const {waitUntilExit} = render(<App python={args.python} cwd={args.cwd} />, {
  exitOnCtrlC: false,
});

waitUntilExit().catch(() => process.exit(1));
