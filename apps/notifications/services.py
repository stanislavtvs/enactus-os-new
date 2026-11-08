import logging
from datetime import date, datetime, timedelta, timezone as datetime_timezone
from uuid import uuid4

from django.db import transaction
from django.db.models import F, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationInvitation, OrganizationMembership

from . import platform
from .models import Notification, NotificationEvent
from .module_scope import exclude_disabled_events

logger = logging.getLogger(__name__)
PREJOIN_KIND = "organization.invited"
OWNERSHIP_KINDS = frozenset({
    "organization.ownership_offered", "organization.ownership_accepted",
    "organization.ownership_declined", "organization.ownership_revoked",
})
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
MAX_DELIVERY_ATTEMPTS = 5
INITIAL_RETRY_SECONDS = 60
SAFE_REMINDER_INTEGERS = frozenset({"offset_minutes", "schedule_revision", "occurrence_revision"})
REMINDER_KINDS = frozenset({"task.reminder", "task.overdue", "calendar.reminder"})


def _utc_timestamp(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime) and timezone.is_aware(value):
            return value.astimezone(datetime_timezone.utc).isoformat()
    except (OverflowError, ValueError):
        pass
    return None


def _technical_data(data):
    data = data or {}
    safe = {
        key: value for key, value in data.items()
        if key in SAFE_TECHNICAL_IDS and type(value) is int and 0 < value <= 2**63 - 1
    }
    safe.update({
        key: value for key, value in data.items()
        if key in SAFE_REMINDER_INTEGERS and type(value) is int and 0 <= value <= 2**63 - 1
    })
    for key in ("planned_at", "reference_at"):
        timestamp = _utc_timestamp(data.get(key))
        if timestamp is not None:
            safe[key] = timestamp
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
    from .policy import policy_allows, policy_for
    from .preferences import preference_allows, preferences_for

    required = event.object_type == "ownership_transfer" and event.kind in OWNERSHIP_KINDS
    if not required and not policy_allows(policy_for(event.organization_id), kind=event.kind, category=event.category):
        return
    recipient_ids = _eligible_recipients(
        event.organization_id, event.kind, event.object_type, event.object_id, event.recipient_ids,
    )
    preferences = {} if required or event.kind == PREJOIN_KIND else preferences_for(event.organization_id, recipient_ids)
    Notification.objects.bulk_create([
        Notification(organization_id=event.organization_id, event=event, recipient_id=recipient_id)
        for recipient_id in recipient_ids
        if preference_allows(preferences.get(recipient_id), kind=event.kind, category=event.category)
    ], batch_size=1000, ignore_conflicts=True)


def _delivery_gate(event, now):
    if event.kind not in REMINDER_KINDS:
        return True, None
    from .reminders import reminder_delivery

    return reminder_delivery(event, now)


def _process_event(event, now):
    if event.delivered_at is not None:
        return event
    if event.organization.disabled_modules and not exclude_disabled_events(
        NotificationEvent.objects.filter(pk=event.pk),
    ).exists():
        return event

    error = None
    try:
        with transaction.atomic():
            allowed, defer_until = _delivery_gate(event, now)
            if allowed:
                _deliver_notifications(event)
    except Exception as exc:
        error = exc
    else:
        if not allowed and defer_until is not None:
            if event.failed_at is None:
                event.next_attempt_at = max(defer_until, now + timedelta(seconds=1))
                event.save(update_fields=["next_attempt_at"])
            return event

    event.delivery_attempts += 1
    event.last_attempt_at = now
    if error is not None:
        # A delivery failure must leave an observable, retryable event
        # without committing partial recipient rows.
        event.last_error = type(error).__name__[:80]
        if event.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
            event.failed_at = event.failed_at or now
            event.next_attempt_at = None
        else:
            delay = INITIAL_RETRY_SECONDS * 2 ** (event.delivery_attempts - 1)
            event.next_attempt_at = now + timedelta(seconds=delay)
        outcome = "failed" if event.failed_at is not None else "postponed"
        logger.warning("Notification event %s delivery %s (%s)", event.pk, outcome, event.last_error)
    else:
        event.delivered_at = now
        event.failed_at = None
        event.next_attempt_at = None
        event.last_error = ""
    event.save(update_fields=[
        "delivery_attempts", "last_attempt_at", "last_error", "delivered_at", "failed_at", "next_attempt_at",
    ])
    return event


@transaction.atomic
def deliver_event(event_id, *, now=None):
    event = NotificationEvent.objects.select_for_update().get(pk=event_id)
    return _process_event(event, now if now is not None else timezone.now())


@transaction.atomic
def publish(
    *, organization_id, actor_id, kind, object_id, recipient_ids, object_type,
    key=None, data=None, deliver_after=None, now=None,
):
    now = now if now is not None else timezone.now()
    category = CATEGORIES[kind.split(".", 1)[0]]
    key = key if key is not None else uuid4().hex
    targets = set(recipient_ids)
    if object_type != "ownership_transfer" or kind not in OWNERSHIP_KINDS:
        targets.discard(actor_id)
    snapshot = _eligible_recipients(organization_id, kind, object_type, object_id, sorted(targets))
    event, created = NotificationEvent.objects.get_or_create(
        organization_id=organization_id, idempotency_key=key,
        defaults={
            "actor_id": actor_id, "kind": kind, "category": category,
            "object_type": object_type, "object_id": object_id,
            "data": _technical_data(data), "recipient_ids": snapshot,
            "next_attempt_at": deliver_after if deliver_after is not None and deliver_after > now else None,
        },
    )
    if (event.kind, event.object_type, event.object_id) != (kind, object_type, object_id):
        raise ValueError("Notification idempotency key already refers to a different operation.")
    if not created or event.next_attempt_at is not None:
        return event
    return _process_event(event, now)


def _retry(limit, *, due_only, now):
    if not 1 <= limit <= 1000:
        raise ValueError("Retry limit must be between 1 and 1000.")
    rows = exclude_disabled_events(NotificationEvent.objects.filter(delivered_at__isnull=True))
    if due_only:
        rows = rows.filter(failed_at__isnull=True).filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
        )
    rows = rows.order_by(F("next_attempt_at").asc(nulls_first=True), "pk")
    attempted = set()
    delivered = failed = 0
    for _ in range(limit):
        with transaction.atomic():
            event = rows.exclude(pk__in=attempted).select_for_update(of=("self",), skip_locked=True).first()
            if event is None:
                break
            attempted.add(event.pk)
            if due_only and event.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
                # Pending records from before retry scheduling can already
                # have exhausted the automatic attempt budget.
                event.failed_at = event.last_attempt_at or now
                event.next_attempt_at = None
                event.save(update_fields=["failed_at", "next_attempt_at"])
            else:
                _process_event(event, now)
            delivered += event.delivered_at is not None
            failed += event.failed_at is not None
    return {
        "processed": len(attempted), "delivered": delivered,
        "pending": len(attempted) - delivered - failed, "failed": failed,
    }


def retry_due(limit=100, *, now=None):
    return _retry(limit, due_only=True, now=now if now is not None else timezone.now())


def retry_pending(limit=100, *, now=None):
    return _retry(limit, due_only=False, now=now if now is not None else timezone.now())


def _recipient_rows(user):
    if not user.is_authenticated or not user.is_active:
        return Notification.objects.none()
    return exclude_disabled_events(Notification.objects.filter(recipient=user), "event__")


def _filtered_rows(user, *, category=None, org_id=None):
    rows = _recipient_rows(user)
    if category is not None:
        rows = rows.filter(event__category=category)
    if org_id is not None:
        rows = rows.filter(organization_id=org_id)
    return rows


def unread_count(*, user, category=None, org_id=None):
    return (
        _filtered_rows(user, category=category, org_id=org_id).filter(read_at__isnull=True).count()
        + platform.recipient_rows(user, category=category, org_id=org_id).filter(read_at__isnull=True).count()
    )


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
    platform_rows = platform.recipient_rows(user, category=category, org_id=org_id)
    if unread:
        rows = rows.filter(read_at__isnull=True)
        platform_rows = platform_rows.filter(read_at__isnull=True)
    offset = (page - 1) * page_size
    platform_total = platform_rows.count()
    references = None
    related = rows.select_related("event__organization", "event__actor")
    if platform_total:
        references = list(
            rows.order_by().annotate(notice_id=F("pk")).values_list("created_at", "notice_id").union(
                platform_rows.order_by().annotate(notice_id=-F("pk")).values_list("created_at", "notice_id"),
                all=True,
            ).order_by("-created_at", "-notice_id")[offset:offset + page_size]
        )
        items = list(related.filter(pk__in=[pk for _, pk in references if pk > 0]))
    else:
        items = list(related.order_by("-pk")[offset:offset + page_size])
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
    if references is not None:
        by_id = {item["id"]: item for item in data}
        by_id.update({
            -item.pk: platform.item_output(item)
            for item in platform_rows.filter(pk__in=[-pk for _, pk in references if pk < 0])
        })
        data = [by_id[pk] for _, pk in references if pk in by_id]
    return {
        "items": data, "total": rows.count() + platform_total, "page": page, "page_size": page_size,
        "organizations": _organization_choices(user) if user.is_authenticated and user.is_active else [],
    }


@transaction.atomic
def mark_read(*, user, notification_id):
    if notification_id < 0:
        rows, pk = platform.recipient_rows(user), -notification_id
    else:
        rows, pk = _recipient_rows(user), notification_id
    item = get_object_or_404(rows, pk=pk)
    if item.read_at is None:
        rows.filter(pk=pk, read_at__isnull=True).update(read_at=timezone.now())
        item.refresh_from_db(fields=["read_at"])
    return {"id": notification_id, "read_at": item.read_at, "unread": unread_count(user=user)}


@transaction.atomic
def mark_all_read(*, user, category=None, org_id=None):
    updated = _filtered_rows(user, category=category, org_id=org_id).filter(
        read_at__isnull=True,
    ).update(read_at=timezone.now())
    updated += platform.recipient_rows(user, category=category, org_id=org_id).filter(
        read_at__isnull=True,
    ).update(read_at=timezone.now())
    return {"updated": updated, "unread": unread_count(user=user)}


def notification_link(*, user, notification_id):
    if notification_id < 0:
        return {"url": platform.notice_link(user, notification_id)}
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
