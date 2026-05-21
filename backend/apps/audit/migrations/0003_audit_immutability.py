"""
Backs the audit-log immutability story at TWO independent layers:

  1. A Postgres trigger that RAISES on UPDATE or DELETE.
  2. REVOKE UPDATE, DELETE from app_role.

The trigger fires for ANY connecting role (so even a SQL-injected migrator
connection can't mutate). The grants prevent app_role from even attempting
the operation. Together: belt-and-suspenders.

The trigger uses TG_OP to provide a meaningful error message that surfaces in
test failures, so when this migration is removed in tests by mistake, the test
output points at the right thing.
"""

from django.db import migrations


SQL_FORWARD = r"""
    CREATE OR REPLACE FUNCTION audit_log_block_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only; % is not permitted', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER audit_log_immutable_trg
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW
    EXECUTE FUNCTION audit_log_block_mutation();

    REVOKE UPDATE, DELETE ON audit_log FROM app_role;
"""

SQL_REVERSE = r"""
    DROP TRIGGER IF EXISTS audit_log_immutable_trg ON audit_log;
    DROP FUNCTION IF EXISTS audit_log_block_mutation();
    GRANT UPDATE, DELETE ON audit_log TO app_role;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0002_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=SQL_FORWARD, reverse_sql=SQL_REVERSE),
    ]
