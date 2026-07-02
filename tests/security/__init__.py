"""Red-team / security regression suite.

Every test in this package is auto-tagged with the ``security`` marker (see
``conftest.py``), so the whole suite can be run in isolation with::

    pytest -m security

Layout and intent are described in the security hardening plan
(``~/.claude/plans/coderai-security-hardening-plan.md``). Each phase of that
plan lands its regression tests here; the shared fixtures they rely on live in
``tests/security/conftest.py``.
"""
