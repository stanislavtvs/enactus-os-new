import logging
from datetime import date
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationInvitation, OrganizationMembership

from .models import Notification, NotificationEvent

logger = logging.getLogger(__name__)
PREJOIN_KIND = "organization.invited"
CATEGORIES = {
    "task": NotificationEvent.Category.TASKS,
    "review": NotificationEvent.Category.REVIEWS,
    "project": NotificationEvent.Category.PROJECTS,
    "staffing": NotificationEvent.Category.PROJECTS,
    "calendar": NotificationEvent.Category.CALENDAR,
    "organization": NotificationEvent.Category.ORGANIZATION,
    "department": NotificationEvent.Category.ORGANIZATION,
}
SAFE_TECHNICAL_IDS = frozenset({
    "stage_id", "request_id", "allocation_id", "invitation_id", "review_id",
})
NEUTRAL_TITLE = "Объект больше недоступен"


def _technical_data(data):
    data = data or {}
    safe = {
        key: value for key, value in data.items()
        if key in SAFE_TECHNICAL_IDS and type(value) is int and 0 < value <= 2**63 - 1
    }
    occurrence_key = data.get("occurrence_key")
    if type(occurrence_key) is date:
        safe["occurrence_key"] = occurrence_key.isoformat()
    elif isinstance(occurrence_key, str):
        try:
            key = date.fromisoformat(occurrence_key)
        except ValueError:
            pass
        else:
            if key.isoformat() == occurrence_key:
                safe["occurrence_key"] = occurrence_key
    return safe


def _eligible_recipients(organization_id, kind, object_type, object_id, recipient_ids):
    if not recipient_ids:
        return []
    if kind == PREJOIN_KIND and object_type == "organization_invitation":
        return list(OrganizationInvitation.objects.filter(
            organization_id=organization_id, pk=object_id, recipient_id__in=recipient_ids,
            recipient__is_active=True, status=OrganizationInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).values_list("recipient_id", flat=True))
    return list(OrganizationMembership.objects.filter(
        organization_id=organization_id, user_id__in=recipient_ids, is_active=True, user__is_active=True,
    ).order_by("user_id").values_list("user_id", flat=True))


def _deliver_notifications(event):
    recipient_ids = _eligible_recipients(
        event.organization_id, event.kind, event.object_type, event.object_id, event.recipient_ids,
    )
    Notification.objects.bulk_create([
        Notification(organization_id=event.organization_id, event=event, recipient_id=recipient_id)
        for recipient_id in recipient_ids
    ], batch_size=1000, ignore_conflicts=True)


@transaction.atomic
def deliver_event(event_id):
    event = NotificationEvent.objects.select_for_update().get(pk=event_id)
    if event.delivered_at is not None:
        return event

    now = timezone.now()
    event.delivery_attempts += 1
    event.last_attempt_at = now
    try:
        with transaction.atomic():
            _deliver_notifications(event)
    except Exception as exc:
        # A delivery failure must leave an observable, retryable event
        # without committing partial recipient rows.
        event.last_error = type(exc).__name__[:80]
        logger.warning("Notification event %s delivery postponed (%s)", event.pk, event.last_error)
    else:
        event.delivered_at = now
        event.last_error = ""
    event.save(update_fields=["delivery_attempts", "last_attempt_at", "last_error", "delivered_at"])
    return event


@transaction.atomic
def publish(*, organization_id, actor_id, kind, object_id, recipient_ids, object_type, key=None, data=None):
    category = CATEGORIES[kind.split(".", 1)[0]]
    key = key if key is not None else uuid4().hex
    snapshot = _eligible_recipients(
        organization_id, kind, object_type, object_id, sorted(set(recipient_ids) - {actor_id}),
    )
    event, _ = NotificationEvent.objects.get_or_create(
        organization_id=organization_id, idempotency_key=key,
        defaults={
            "actor_id": actor_id, "kind": kind, "category": category,
            "object_type": object_type, "object_id": object_id,
            "data": _technical_data(data), "recipient_ids": snapshot,
        },
    )
    if (event.kind, event.object_type, event.object_id) != (kind, object_type, object_id):
        raise ValueError("Notification idempotency key already refers to a different operation.")
    return deliver_event(event.pk)


def retry_pending(limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("Retry limit must be between 1 and 1000.")
    ids = list(NotificationEvent.objects.filter(delivered_at__isnull=True).order_by("pk").values_list(
        "pk", flat=True,
    )[:limit])
    delivered = sum(deliver_event(event_id).delivered_at is not None for event_id in ids)
    return {"processed": len(ids), "delivered": delivered, "pending": len(ids) - delivered}


def _recipient_rows(user):
    if not user.is_authenticated or not user.is_active:
        return Notification.objects.none()
    return Notification.objects.filter(recipient=user)


def _filtered_rows(user, *, category=None, org_id=None):
    rows = _recipient_rows(user)
    if category is not None:
        rows = rows.filter(event__category=category)
    if org_id is not None:
        rows = rows.filter(organization_id=org_id)
    return rows


def unread_count(*, user, category=None, org_id=None):
    return _filtered_rows(user, category=category, org_id=org_id).filter(read_at__isnull=True).count()


def _organization_choices(user):
    invitations = OrganizationInvitation.objects.filter(
        recipient=user, status=OrganizationInvitation.Status.PENDING, expires_at__gt=timezone.now(),
    ).values("organization_id")
    memberships = OrganizationMembership.objects.filter(
        user=user, is_active=True,
    ).values("organization_id")
    return list(Organization.objects.filter(
        Q(pk__in=memberships) | Q(pk__in=invitations),
    ).order_by("name", "pk").values("id", "name"))


def _present(user, notifications):
    from .visibility import presentation

    events = {notification.event_id: notification.event for notification in notifications}
    return presentation(user, list(events.values())) if events else {}


def list_notifications(*, user, category=None, org_id=None, unread=False, page=1, page_size=20):
    rows = _filtered_rows(user, category=category, org_id=org_id)
    if unread:
        rows = rows.filter(read_at__isnull=True)
    offset = (page - 1) * page_size
    items = list(rows.select_related("event__organization", "event__actor").order_by("-pk")[offset:offset + page_size])
    presented = _present(user, items)
    data = []
    for item in items:
        detail = presented.get(item.event_id) or {}
        name = detail.get("organization_name")
        data.append({
            "id": item.pk,
            "kind": item.event.kind,
            "category": item.event.category,
            "organization_id": item.organization_id if name is not None else None,
            "organization_name": name,
            "title": detail.get("title") or NEUTRAL_TITLE,
            "description": detail.get("description") or "",
            "created_at": item.created_at,
            "read_at": item.read_at,
            "available": bool(detail.get("url")),
        })
    return {
        "items": data, "total": rows.count(), "page": page, "page_size": page_size,
        "organizations": _organization_choices(user) if user.is_authenticated and user.is_active else [],
    }


@transaction.atomic
def mark_read(*, user, notification_id):
    rows = _recipient_rows(user)
    item = get_object_or_404(rows, pk=notification_id)
    if item.read_at is None:
        rows.filter(pk=notification_id, read_at__isnull=True).update(read_at=timezone.now())
        item.refresh_from_db(fields=["read_at"])
    return {"id": item.pk, "read_at": item.read_at, "unread": unread_count(user=user)}


@transaction.atomic
def mark_all_read(*, user, category=None, org_id=None):
    updated = _filtered_rows(user, category=category, org_id=org_id).filter(
        read_at__isnull=True,
    ).update(read_at=timezone.now())
    return {"updated": updated, "unread": unread_count(user=user)}


def notification_link(*, user, notification_id):
    item = get_object_or_404(
        _recipient_rows(user).select_related("event__organization", "event__actor"), pk=notification_id,
    )
    url = (_present(user, [item]).get(item.event_id) or {}).get("url")
    if (
        not isinstance(url, str) or not url.startswith("/") or url.startswith("//")
        or "\\" in url or any(ord(character) < 32 or ord(character) == 127 for character in url)
    ):
        raise Http404
    return {"url": url}
