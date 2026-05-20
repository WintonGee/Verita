"""
Test-mode settings. Inherits everything from settings.py, but flattens the
role-split for ORM-level tests (so pytest-django can create + migrate the
test DB without role gymnastics).

The role-split behavior itself is exercised in a small set of integration
tests (apps/audit/tests/test_role_split.py) that connect via raw psycopg
as app_role and verify the trigger + grants are real.
"""

from .settings import *  # noqa: F401,F403

# Single connection for tests, using migrator_role's credentials.
# Both 'default' (ORM runtime) and 'migrator' (migrations) point at the same
# Postgres connection, so test setup is trivial.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "verita_test",
        "USER": "migrator_role",
        "PASSWORD": "migrator_pass",
        "HOST": "postgres",
        "PORT": "5432",
        "TEST": {"NAME": "verita_test"},
    },
}
# Drop the router during tests — single connection.
DATABASE_ROUTERS = []
