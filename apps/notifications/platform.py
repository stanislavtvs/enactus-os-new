from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse

from apps.organizations.platform_access import is_platform_admin
from apps.organizations.platform_models import PlatformNotice


TITLES = {
    "platform.creation_submitted": "Новая заявка на создание организации",
    "platform.creation_approved": "Заявка на создание организации одобрена",
    "platform.creation_rejected": "Заявка на создание организации отклонена",
}


def notify_request(*, request, kind, recipients):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Platform notices must share the business transaction.")
    if kind not in TITLES:
        raise ValueError("Unknown platform notice kind.")
    PlatformNotice.objects.bulk_create([
        PlatformNotice(recipient_id=recipient, kind=kind, creation_request=request)
        for recipient in set(recipients)
    ], ignore_conflicts=True)


def recipient_rows(user, *, category=None, org_id=None):
    if org_id is not None or category not in (None, "organization") or not user.is_authenticated or not user.is_active:
        return PlatformNotice.objects.none()
    rows = PlatformNotice.objects.filter(recipient=user)
    if is_platform_admin(user):
        return rows.filter(kind__in=TITLES)
    return rows.filter(
        creation_request__applicant=user, kind__in=("platform.creation_approved", "platform.creation_rejected"),
    )


def item_output(item):
    return {
        "id": -item.pk,
        "kind": item.kind,
        "category": "organization",
        "organization_id": None,
        "organization_name": None,
        "title": TITLES[item.kind],
        "description": "",
        "created_at": item.created_at,
        "read_at": item.read_at,
        "available": True,
    }


def notice_link(user, notice_id):
    item = get_object_or_404(recipient_rows(user), pk=-notice_id)
    if item.kind == "platform.creation_submitted":
        return reverse("platform-management") + "#platform-requests-heading"
    return reverse("organization-list") + "#organization-requests"
