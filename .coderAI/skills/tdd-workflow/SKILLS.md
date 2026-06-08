---
name: tdd-workflow
description: Test-Driven Development cycle
---

# TDD Workflow

This skill defines the structured Red-Green-Refactor development loop. Agents should follow these steps when implementing logic changes to ensure high reliability.

## Step 1: Write a Failing Test (RED)
- Identify the expected behavior of the feature or bug fix.
- Create or modify a test file in the `tests/` directory.
- Write a clear, focused test that asserts the expected outcome.
- Ensure the test checks for both happy paths and edge cases (e.g., null inputs, empty strings, boundaries).

## Step 2: Run the Test
- Execute the test suite using `pytest`.
- Confirm that the new test FAILS. If it passes, the test is either testing the wrong thing, or the behavior is already implemented.

## Step 3: Implement Minimal Code (GREEN)
- Write the minimum amount of code required in the implementation file to make the test pass.
- Do not add extra features or optimize prematurely.

## Step 4: Run the Test Again
- Execute `pytest` again.
- Confirm that the test now PASSES along with all other existing tests.

## Step 5: Refactor (IMPROVE)
- Review the implemented code and the test.
- Improve variable names, remove duplication, and optimize logic.
- Run `pytest` one final time to ensure the refactoring did not break any tests.
