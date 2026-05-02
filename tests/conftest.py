"""Shared test setup."""

import os

# Filesystem tools refuse writes outside the project root by default. Tests
# write to pytest's ``tmp_path`` (typically /tmp/...), which is outside this
# repo, so opt out for the test session.
os.environ["CODERAI_ALLOW_OUTSIDE_PROJECT"] = "1"
