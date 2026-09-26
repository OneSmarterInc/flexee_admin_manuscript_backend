from __future__ import annotations

from typing import Any

from .auth import remote_hash
from .models import AuditEvent


def _role_for_organization(user, organization_id):
    if not user:
        return ''
    if getattr(user, 'platform_superuser', False):
        return 'platform_superuser'
    if not organization_id:
        return ''
    membership = user.memberships.filter(organization_id=organization_id).first()
    return membership.role if membership else ''


def record_audit_event(
    request,
    action: str,
    *,
    resource_type: str = '',
    resource_id: Any = '',
    organization_id=None,
    venue_id=None,
    venue_submission_id=None,
    manuscript_id=None,
    detail: dict | None = None,
):
    """Persist one append-only admin/editor audit event.

    The log stores a hashed remote address rather than a raw IP and snapshots
    the actor's email/role so the event remains useful even if memberships
    change later.
    """
    user = getattr(request, 'editor_user', None)
    actor_email = ''
    actor_id = None
    actor_role = ''
    if user is not None:
        actor_email = str(user.email or '').strip()
        actor_id = user.id
        actor_role = _role_for_organization(user, organization_id)

    return AuditEvent.objects.create(
        actor_id=actor_id,
        actor_email=actor_email,
        actor_role=actor_role,
        action=str(action or '').strip()[:120],
        resource_type=str(resource_type or '').strip()[:80],
        resource_id=str(resource_id or '').strip()[:100],
        organization_id=organization_id,
        venue_id=venue_id,
        venue_submission_id=venue_submission_id,
        manuscript_id=manuscript_id,
        remote_hash=remote_hash(request),
        detail=detail if isinstance(detail, dict) else {},
    )


def audit_event_payload(event):
    return {
        'id': str(event.id),
        'occurred_at': event.occurred_at.isoformat(),
        'actor_id': str(event.actor_id) if event.actor_id else None,
        'actor_email': event.actor_email,
        'actor_role': event.actor_role,
        'action': event.action,
        'resource_type': event.resource_type,
        'resource_id': event.resource_id,
        'organization_id': str(event.organization_id) if event.organization_id else None,
        'venue_id': str(event.venue_id) if event.venue_id else None,
        'venue_submission_id': str(event.venue_submission_id) if event.venue_submission_id else None,
        'manuscript_id': str(event.manuscript_id) if event.manuscript_id else None,
        'detail': event.detail,
    }
