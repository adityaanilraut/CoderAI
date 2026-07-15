---
name: tdd-workflow
description: Red-Green-Refactor workflow for behavior changes where a focused regression test is practical.
---

# TDD Workflow

Use this workflow for logic changes and bug fixes when a focused test can demonstrate the behavior. For configuration, documentation, or hard-to-reproduce integration work, use the narrowest meaningful verification instead of forcing a synthetic test.

## Step 1: Write a Failing Test (RED)
- Identify the expected behavior of the feature or bug fix.
- Create or modify a test file in the `tests/` directory.
- Write a clear, focused test that asserts the expected outcome.
- Cover the changed behavior and the most relevant edge case; avoid unrelated test expansion.

## Step 2: Run the Test
- Run the project's narrowest relevant test command.
- Confirm that the new test FAILS. If it passes, the test is either testing the wrong thing, or the behavior is already implemented.

## Step 3: Implement Minimal Code (GREEN)
- Write the minimum amount of code required in the implementation file to make the test pass.
- Do not add extra features or optimize prematurely.

## Step 4: Run the Test Again
- Re-run the same focused test, then the broader relevant suite when practical.
- Confirm that the test now PASSES along with all other existing tests.

## Step 5: Refactor (IMPROVE)
- Review the implemented code and the test.
- Improve variable names, remove duplication, and optimize logic.
- Re-run the relevant checks after refactoring.
