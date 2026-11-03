"""Shared test setup: force the test environment before any app module is imported."""

import os

# Must run before `app.core.config` is imported: it reads APP_ENV at import time,
# and the test environment skips the production JWT secret check.
os.environ["APP_ENV"] = "test"
os.environ.setdefault("GEMINI_API_KEY", "test-key-never-sent")
