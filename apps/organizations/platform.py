from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.audit.platform import record_platform
from apps.notifications.platform import notify_request

from .models import OrganizationCreationRequest, PlatformCreationGrant, PlatformCreationPolicy
from .platform_access import PLATFORM_ADMIN_GROUP, is_platform_admin

REQUEST_DESCRIPTION_MAX_LENGTH = 2000
DECISION_COMMENT_MAX_LENGTH = 1000
REQUESTS_PER_DAY = 5


class CreationConflict(Exception):
    """A creation operation is incompatible with the current state."""


class CreationValidation(Exception):
    """Invalid user-supplied creation data."""


class CreationRateLimited(CreationConflict):
    """Too many creation requests in the rolling window."""


def valid_text(value):
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def policy_row(*, lock=False):
    if lock:
        policy, _ = PlatformCreationPolicy.objects.select_for_update().get_or_create(pk=1)
        return policy
    return PlatformCreationPolicy.objects.filter(pk=1).first() or PlatformCreationPolicy(pk=1)


def _active(user):
    return user.is_authenticated and user.is_active and not user.registration_pending


def _verified(user):
    return _active(user) and user.email_verified_at is not None


def may_create(user, mode):
    if not _active(user):
        return False
    if mode == PlatformCreationPolicy.Mode.OPEN:
        return _verified(user)
    if mode == PlatformCreationPolicy.Mode.PERMISSION_ONLY:
        return user.has_perm("organizations.add_organization") or (
            _verified(user) and PlatformCreationGrant.objects.filter(user=user, revoked_at__isnull=True).exists()
        )
    return False


def _policy_data(user, policy):
    return {
        "mode": policy.mode,
        "can_create": may_create(user, policy.mode),
        "can_request": _verified(user) and policy.mode == PlatformCreationPolicy.Mode.REQUEST_APPROVAL,
        "can_administer": is_platform_admin(user),
        "email_verified": _verified(user),
    }


def creation_policy(user):
    return _policy_data(user, policy_row())


def _require_admin(user):
    if not is_platform_admin(user):
        raise PermissionDenied


@transaction.atomic
def set_creation_policy(*, user, mode):
    _require_admin(user)
    policy = policy_row(lock=True)
    if mode not in PlatformCreationPolicy.Mode.values:
        raise CreationValidation("Неизвестный режим создания организаций.")
    if policy.mode != mode:
        previous = policy.mode
        policy.mode = mode
        policy.changed_by = user
        policy.save(update_fields=["mode", "changed_by", "changed_at"])
        record_platform(
            actor=user, action="platform.policy_changed", subject_id=policy.pk,
            metadata={"old_mode": previous, "new_mode": mode},
        )
    return _policy_data(user, policy)


def _members():
    return get_user_model().objects.filter(is_active=True, registration_pending=False)


def _admin_recipients():
    return _members().filter(groups__name=PLATFORM_ADMIN_GROUP).values_list("pk", flat=True)


def _grant_rows():
    return PlatformCreationGrant.objects.select_related("user", "granted_by", "revoked_by")


def list_creation_grants(*, user, q=""):
    _require_admin(user)
    rows = _grant_rows()
    if q:
        rows = rows.filter(user__username__icontains=q)
    return rows.order_by("user__username", "pk")


def grant_candidates(*, user, q):
    _require_admin(user)
    if not q:
        return _members().none()
    return _members().filter(email_verified_at__isnull=False, username__icontains=q).exclude(
        pk=user.pk,
    ).order_by("username", "pk")[:20]


@transaction.atomic
def grant_creation(*, user, username):
    _require_admin(user)
    policy_row(lock=True)
    if username == user.get_username():
        raise PermissionDenied
    target = get_object_or_404(
        _members().select_for_update().filter(email_verified_at__isnull=False), username=username,
    )
    if target.pk == user.pk:
        raise PermissionDenied
    grant = _grant_rows().filter(user=target).first()
    if grant is None:
        grant = PlatformCreationGrant.objects.create(user=target, granted_by=user)
    elif grant.revoked_at is not None:
        grant.granted_by = user
        grant.granted_at = timezone.now()
        grant.revoked_by = None
        grant.revoked_at = None
        grant.save(update_fields=["granted_by", "granted_at", "revoked_by", "revoked_at"])
    else:
        return grant
    record_platform(actor=user, action="platform.creation_granted", subject_id=target.pk)
    return grant


@transaction.atomic
def revoke_creation(*, user, user_id):
    _require_admin(user)
    policy_row(lock=True)
    if user_id == user.pk:
        raise PermissionDenied
    grant = get_object_or_404(_grant_rows().select_for_update(of=("self",)), user_id=user_id)
    if grant.revoked_at is None:
        grant.revoked_by = user
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["revoked_by", "revoked_at"])
        record_platform(actor=user, action="platform.creation_revoked", subject_id=user_id)


def _request_rows():
    return OrganizationCreationRequest.objects.select_related("applicant", "reviewer").order_by("-created_at", "-pk")


def own_requests(*, user, status=None):
    if not _active(user):
        raise PermissionDenied
    rows = _request_rows().filter(applicant=user)
    return rows.filter(status=status) if status else rows


def admin_requests(*, user, status=None, q=""):
    _require_admin(user)
    rows = _request_rows()
    if status:
        rows = rows.filter(status=status)
    if q:
        rows = rows.filter(Q(name__icontains=q) | Q(applicant__username__icontains=q))
    return rows


def admin_request(*, user, request_id):
    return get_object_or_404(admin_requests(user=user), pk=request_id)


@transaction.atomic
def submit_request(*, user, name, description):
    from .services import clean_organization_name

    policy = policy_row(lock=True)
    applicant = _members().select_for_update().filter(pk=user.pk).first()
    if applicant is None or not _verified(applicant):
        raise PermissionDenied
    if policy.mode != PlatformCreationPolicy.Mode.REQUEST_APPROVAL:
        raise CreationConflict("Сейчас создание организаций по заявкам отключено.")
    name = clean_organization_name(name)
    description = description.strip()
    if not description or len(description) > REQUEST_DESCRIPTION_MAX_LENGTH or not valid_text(description):
        raise CreationValidation("Укажите цель создания организации.")
    previous = OrganizationCreationRequest.objects.filter(applicant=applicant)
    if previous.filter(status=OrganizationCreationRequest.Status.PENDING).exists():
        raise CreationConflict("У вас уже есть ожидающая заявка.")
    if previous.filter(created_at__gte=timezone.now() - timedelta(days=1)).count() >= REQUESTS_PER_DAY:
        raise CreationRateLimited("Слишком много заявок. Повторите позже.")
    request = OrganizationCreationRequest.objects.create(
        applicant=applicant, name=name, description=description,
    )
    record_platform(actor=user, action="platform.creation_submitted", subject_id=request.pk)
    notify_request(
        request=request, kind="platform.creation_submitted", recipients=_admin_recipients().exclude(pk=user.pk),
    )
    return request


def _require_pending(request):
    if request.status != OrganizationCreationRequest.Status.PENDING:
        raise CreationConflict("Заявка уже обработана или отозвана.")


@transaction.atomic
def withdraw_request(*, user, request_id):
    request = get_object_or_404(
        own_requests(user=user).select_for_update(of=("self",)), pk=request_id,
    )
    _require_pending(request)
    request.status = OrganizationCreationRequest.Status.WITHDRAWN
    request.decided_at = timezone.now()
    request.save(update_fields=["status", "decided_at"])
    record_platform(actor=user, action="platform.creation_withdrawn", subject_id=request.pk)
    return request


@transaction.atomic
def decide_request(*, user, request_id, decision, comment=""):
    from .services import _create_organization_for_owner

    _require_admin(user)
    policy = policy_row(lock=True)
    request = get_object_or_404(_request_rows().select_for_update(of=("self",)), pk=request_id)
    if request.applicant_id == user.pk:
        raise PermissionDenied
    _require_pending(request)
    if decision not in {"approve", "reject"}:
        raise CreationValidation("Выберите решение по заявке.")
    comment = comment.strip()
    if len(comment) > DECISION_COMMENT_MAX_LENGTH or not valid_text(comment) or (decision == "reject" and not comment):
        raise CreationValidation("Укажите причину отклонения заявки.")
    if decision == "approve":
        if policy.mode != PlatformCreationPolicy.Mode.REQUEST_APPROVAL:
            raise CreationConflict("Сейчас создание организаций по заявкам отключено.")
        applicant = _members().select_for_update().filter(pk=request.applicant_id).first()
        if applicant is None or not _verified(applicant):
            raise CreationConflict("Аккаунт заявителя больше не может создать организацию.")
        request.organization = _create_organization_for_owner(user=applicant, name=request.name, actor=user)
        request.status = OrganizationCreationRequest.Status.APPROVED
    else:
        request.status = OrganizationCreationRequest.Status.REJECTED
    request.reviewer = user
    request.decided_at = timezone.now()
    request.decision_comment = comment
    request.save(update_fields=["status", "organization", "reviewer", "decided_at", "decision_comment"])
    action = f"platform.creation_{request.status}"
    record_platform(
        actor=user, action=action, subject_id=request.pk,
        metadata={"organization_id": request.organization_id} if request.organization_id else None,
    )
    notify_request(request=request, kind=action, recipients=[request.applicant_id])
    return request
