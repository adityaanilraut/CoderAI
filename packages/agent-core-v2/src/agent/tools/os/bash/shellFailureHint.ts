export function shellFailureHint(output: string): string | undefined {
  if (/(?:^|\n)[^\n]*\bpython: (?:command not found|not found)\s*(?:\n|$)/.test(output)) {
    return 'The Python interpreter was not found, so no tests ran. Use the project\'s configured interpreter or virtual environment; on macOS/Linux, python3 is usually available even when python is not.';
  }

  if (
    /(?:PermissionError:|Operation not permitted|Permission denied)/i.test(output) &&
    /(?:[\\/]_pytest[\\/]|[\\/]pytest[\\/]|\bpytest\.ini\b)/.test(output) &&
    !/(?:^|\n)_{3,} (?!ERROR collecting\b).+ _{3,}(?:\n|$)/.test(output)
  ) {
    return 'pytest encountered a filesystem permission error during setup or collection; this is not evidence of a test assertion failure. Do not repeat configuration variants against the same restriction. If the existing tests use unittest.TestCase and need no pytest fixtures or plugins, run them with the project\'s Python interpreter and unittest (for example, python3 -m unittest discover -s tests -v). Otherwise report the blocked verification. Keep permission boundaries intact.';
  }

  return undefined;
}
