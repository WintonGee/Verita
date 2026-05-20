"""
Proves audit_log immutability at the trigger layer.

The grant-revocation layer (REVOKE UPDATE, DELETE ON audit_log FROM app_role)
is the second line of defense — exercised separately via raw psycopg in
test_role_split.py because the test DB is created using migrator_role and
doesn't pick up per-DB default privileges. The trigger fires regardless of
role, so it's testable directly from the ORM here.
"""

import pytest
from django.db import transaction, connection

from apps.audit.models import AuditLog


@pytest.fixture
def audit_row(db):
    return AuditLog.objects.create(
        actor_type=AuditLog.ActorType.SYSTEM,
        actor_id="test",
        action="test.create",
        resource_type="test_resource",
        resource_id="abc-123",
        after={"value": 1},
        reason="under test",
    )


@pytest.mark.django_db(transaction=True)
def test_audit_row_can_be_inserted_and_read(audit_row):
    assert AuditLog.objects.get(id=audit_row.id).action == "test.create"


@pytest.mark.django_db(transaction=True)
def test_audit_row_update_is_blocked_by_trigger(audit_row):
    """
    UPDATE attempts must raise. The trigger fires before the row is touched,
    so the after-value should remain unchanged.
    """
    with pytest.raises(Exception) as exc_info:
        with transaction.atomic():
            AuditLog.objects.filter(id=audit_row.id).update(action="tampered")
    # Postgres raises a generic Programming/IntegrityError via psycopg
    assert "append-only" in str(exc_info.value).lower() or \
           "not permitted" in str(exc_info.value).lower()

    # Row must be untouched
    fresh = AuditLog.objects.get(id=audit_row.id)
    assert fresh.action == "test.create"


@pytest.mark.django_db(transaction=True)
def test_audit_row_delete_is_blocked_by_trigger(audit_row):
    with pytest.raises(Exception) as exc_info:
        with transaction.atomic():
            AuditLog.objects.filter(id=audit_row.id).delete()
    assert "append-only" in str(exc_info.value).lower() or \
           "not permitted" in str(exc_info.value).lower()

    # Row still there
    assert AuditLog.objects.filter(id=audit_row.id).exists()


@pytest.mark.django_db(transaction=True)
def test_audit_row_direct_sql_update_is_blocked(audit_row):
    """Same proof at the raw SQL layer — bypasses the ORM entirely."""
    with pytest.raises(Exception):
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE audit_log SET action = 'sql-tamper' WHERE id = %s",
                    [audit_row.id],
                )
    fresh = AuditLog.objects.get(id=audit_row.id)
    assert fresh.action == "test.create"
