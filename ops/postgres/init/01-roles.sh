#!/bin/bash
# Runs once on first Postgres boot (postgres image's docker-entrypoint-initdb.d).
# Creates the role split that backs the audit-log immutability story.
#
#   migrator_role:  DDL — owns the schema, runs Django migrations.
#   app_role:       DML — the runtime Django connection. Cannot UPDATE or DELETE
#                   on audit_log; per-table grants are revoked in the audit_log
#                   migration once that table exists. A trigger is also added
#                   as belt-and-suspenders.

set -e

: "${APP_ROLE_PASSWORD:?APP_ROLE_PASSWORD must be set}"
: "${MIGRATOR_ROLE_PASSWORD:?MIGRATOR_ROLE_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
    -- Migrator gets CREATEDB so pytest-django can spin up the test database
    -- via its own credentials. CREATEDB is narrower than SUPERUSER (which we
    -- avoid) — it only allows database creation, not bypassing other checks.
    CREATE ROLE migrator_role LOGIN PASSWORD '$MIGRATOR_ROLE_PASSWORD' CREATEDB;
    CREATE ROLE app_role LOGIN PASSWORD '$APP_ROLE_PASSWORD';

    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO migrator_role;
    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO app_role;

    ALTER SCHEMA public OWNER TO migrator_role;
    GRANT USAGE ON SCHEMA public TO app_role;

    -- Anything migrator_role creates in the public schema automatically gets
    -- baseline DML grants for app_role. Per-table tightening (revoke UPDATE
    -- on audit_log) happens in the audit_log migration.
    ALTER DEFAULT PRIVILEGES FOR ROLE migrator_role IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_role;
    ALTER DEFAULT PRIVILEGES FOR ROLE migrator_role IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO app_role;
EOSQL

echo "[init] Created migrator_role and app_role with default privileges."
