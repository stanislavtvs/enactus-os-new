from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from ninja import PatchDict, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from . import policy, preferences
from .policy_schemas import (
    NotificationPolicyChange, NotificationPolicyResponse,
    NotificationPreferenceChange, NotificationPreferenceResponse,
)
from .schemas import RecordId

router = Router(auth=django_auth)


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except policy.PolicyValidation as exc:
        raise HttpError(422, str(exc)) from None


@router.get("/{org_id}/notification-policy/", response=NotificationPolicyResponse, url_name="notification_policy")
def get_notification_policy(request, response: HttpResponse, org_id: RecordId):
    response["Cache-Control"] = "no-store"
    return _perform(policy.get_policy, user=request.user, org_id=org_id)


@router.patch("/{org_id}/notification-policy/", response=NotificationPolicyResponse)
def update_notification_policy(
    request, response: HttpResponse, org_id: RecordId, payload: PatchDict[NotificationPolicyChange],
):
    response["Cache-Control"] = "no-store"
    return _perform(policy.update_policy, user=request.user, org_id=org_id, changes=payload)


@router.get(
    "/{org_id}/notification-preferences/", response=NotificationPreferenceResponse,
    url_name="notification_preferences",
)
def get_notification_preferences(request, response: HttpResponse, org_id: RecordId):
    response["Cache-Control"] = "no-store"
    return _perform(preferences.get_preferences, user=request.user, org_id=org_id)


@router.patch("/{org_id}/notification-preferences/", response=NotificationPreferenceResponse)
def update_notification_preferences(
    request, response: HttpResponse, org_id: RecordId, payload: PatchDict[NotificationPreferenceChange],
):
    response["Cache-Control"] = "no-store"
    return _perform(preferences.update_preferences, user=request.user, org_id=org_id, changes=payload)
