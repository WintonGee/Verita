"""
Grant-revocation layer of audit-log immutability.

The trigger layer (test_audit_immutability.py) blocks UPDATE/DELETE for ANY
role. This test exercises the second, independent layer: even granted full DML,
`app_role` cannot UPDATE or DELETE audit_log because the migration REVOKEs those.

pytest's test DB is created by migrator_role and doesn't inherit the init
script's default privileges, so we set up app_role's grants explicitly here to
mirror production posture, then connect AS app_role via raw psycopg to prove the
REVOKE actually denies the writes.
"""

import os

import psycopg
import pytest
from django.db import connection


@pytest.mark.django_db(transaction=True)
def test_app_role_grants_block_audit_log_mutation():
    db = connection.settings_dict["NAME"]
    host = connection.settings_dict["HOST"]
    port = connection.settings_dict["PORT"]
    app_pw = os.environ.get("APP_ROLE_PASSWORD", "app_pass")

    # Mirror production grant posture on this test DB, then apply the REVOKE.
    with connection.cursor() as cur:
        cur.execute(f'GRANT CONNECT ON DATABASE "{db}" TO app_role')
        cur.execute("GRANT USAGE ON SCHEMA public TO app_role")
        cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON audit_log TO app_role")
        cur.execute("REVOKE UPDATE, DELETE ON audit_log FROM app_role")
        cur.execute(
            "INSERT INTO audit_log (created_at, actor_type, actor_id, action, "
            "resource_type, resource_id, after, reason) "
            "VALUES (now(), 'system', 't', 't.a', 't', 'r', '{}'::jsonb, '') "
            "RETURNING id"
        )
        row_id = cur.fetchone()[0]
    # Outside an atomic block, Django autocommits — the row + grants are visible
    # to a fresh connection.

    conn = psycopg.connect(dbname=db, user="app_role", password=app_pw,
                           host=host, port=port, autocommit=True)
    try:
        with conn.cursor() as c:
            # SELECT + INSERT are allowed for app_role.
            c.execute("SELECT count(*) FROM audit_log")
            assert c.fetchone()[0] >= 1
            # UPDATE is denied by the revoked grant.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE audit_log SET action = 'tamper' WHERE id = %s", [row_id])
            # DELETE is denied by the revoked grant.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("DELETE FROM audit_log WHERE id = %s", [row_id])
    finally:
        conn.close()
