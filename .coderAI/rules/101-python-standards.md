# 101: Python Standards

This rule specifies the language guidelines for Python development within this project.

## 1. Type Hinting
- **Typing:** Add type hints to new or changed public interfaces and follow the module's existing typing level.
- **Return Types:** Always specify the return type, even if it is `None`.

## 2. Testing Framework
- **Use Pytest:** All tests should be written using `pytest`. Use fixtures for setup and teardown, and `pytest.mark.parametrize` for testing multiple inputs.
- **Mocking:** Use `unittest.mock` (or `pytest-mock`) to mock external dependencies or expensive operations.

## 3. Formatting and Linting
- **PEP 8:** Follow PEP 8 style guidelines.
- **Docstrings:** Document public APIs when their purpose or contract is not already obvious.

## 4. Error Handling
- **Specific Exceptions:** Catch specific exceptions rather than using broad `except Exception:` blocks unless at the top level of a process.
- **Meaningful Messages:** Provide clear, actionable error messages with context when raising exceptions.
