from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from ninja import Query, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from . import services
from .schemas import AuditDetail, AuditOptions, AuditPage, AuditQuery, RecordId

router = Router(auth=django_auth)


def _read(service, **kwargs):
    try:
        return service(**kwargs)
    except PermissionDenied as exc:
        raise HttpError(403, "Нет доступа к журналу действий.") from exc


@router.get("/{org_id}/audit/", response=AuditPage, url_name="audit_list")
def list_events(request, response: HttpResponse, org_id: RecordId, filters: Query[AuditQuery]):
    response["Cache-Control"] = "no-store"
    return _read(services.list_events, user=request.user, org_id=org_id, filters=filters)


@router.get("/{org_id}/audit/options/", response=AuditOptions, url_name="audit_options")
def options(request, response: HttpResponse, org_id: RecordId):
    response["Cache-Control"] = "no-store"
    return _read(services.audit_options, user=request.user, org_id=org_id)


@router.get("/{org_id}/audit/objects/{object_type}/{object_id}/", response=AuditPage, url_name="audit_history")
def object_history(
    request, response: HttpResponse, org_id: RecordId, object_type: str,
    object_id: RecordId, filters: Query[AuditQuery],
):
    response["Cache-Control"] = "no-store"
    return _read(
        services.list_events, user=request.user, org_id=org_id, filters=filters, object_ref=(object_type, object_id),
    )


@router.get("/{org_id}/audit/{event_id}/", response=AuditDetail, url_name="audit_detail")
def detail(request, response: HttpResponse, org_id: RecordId, event_id: RecordId):
    response["Cache-Control"] = "no-store"
    return _read(services.event_detail, user=request.user, org_id=org_id, event_id=event_id)
