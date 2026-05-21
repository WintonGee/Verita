"""
Test-mode settings. Inherits everything from settings.py, but flattens the
role-split for ORM-level tests (so pytest-django can create + migrate the
test DB without role gymnastics).

The role-split behavior itself is exercised in tests/test_role_split.py, which
connects via raw psycopg as app_role and verifies the grant-revocation layer.
"""

import os
import uuid

from .settings import *  # noqa: F401,F403

# Unique test-DB name per session so concurrent `pytest` invocations (e.g. two
# CI shards, or two engineers) don't collide on a shared `verita_test` and race
# its create/drop. pytest-django creates this DB at session start and drops it
# at the end, so there's no leak under normal completion.
_test_db_name = os.environ.get("TEST_DB_NAME") or f"verita_test_{uuid.uuid4().hex[:8]}"

# Single connection for tests, using migrator_role's credentials.
# Both 'default' (ORM runtime) and 'migrator' (migrations) point at the same
# Postgres connection, so test setup is trivial.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _test_db_name,
        "USER": "migrator_role",
        "PASSWORD": "migrator_pass",
        "HOST": os.environ.get("POSTGRES_HOST", "postgres"),
        "PORT": "5432",
        "TEST": {"NAME": _test_db_name},
    },
}
# Drop the router during tests — single connection.
DATABASE_ROUTERS = []
