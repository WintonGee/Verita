"""
Single chokepoint for writing audit rows. Every money-moving action calls
this inside its own transaction, so the audit row commits atomically with
the effect it records.
"""

from apps.audit.models import AuditLog


def write_audit(*, actor_type, actor_id, action, resource_type, resource_id,
                after, before=None, reason="", request_ip=None):
    return AuditLog.objects.create(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        before=before,
        after=after,
        reason=reason,
        request_ip=request_ip,
    )
