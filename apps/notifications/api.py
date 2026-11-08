from django.http import HttpResponse
from ninja import Query, Router
from ninja.security import django_auth

from . import services
from .schemas import (
    NotificationCount, NotificationFilter, NotificationLink, NotificationList,
    NotificationQuery, NotificationRead, NotificationsRead, NoticeId,
)

router = Router(auth=django_auth)


@router.get("/notifications/", response=NotificationList, url_name="notification_list")
def list_notifications(request, response: HttpResponse, filters: Query[NotificationQuery]):
    response["Cache-Control"] = "no-store"
    return services.list_notifications(user=request.user, **filters.model_dump())


@router.get("/notifications/count/", response=NotificationCount, url_name="notification_count")
def count_notifications(request, response: HttpResponse, filters: Query[NotificationFilter]):
    response["Cache-Control"] = "no-store"
    return {"unread": services.unread_count(user=request.user, **filters.model_dump())}


@router.post("/notifications/{notification_id}/read/", response=NotificationRead)
def mark_notification_read(request, response: HttpResponse, notification_id: NoticeId):
    response["Cache-Control"] = "no-store"
    return services.mark_read(user=request.user, notification_id=notification_id)


@router.post("/notifications/read-all/", response=NotificationsRead)
def mark_notifications_read(request, response: HttpResponse, filters: Query[NotificationFilter]):
    response["Cache-Control"] = "no-store"
    return services.mark_all_read(user=request.user, **filters.model_dump())


@router.get("/notifications/{notification_id}/link/", response=NotificationLink)
def get_notification_link(request, response: HttpResponse, notification_id: NoticeId):
    response["Cache-Control"] = "no-store"
    return services.notification_link(user=request.user, notification_id=notification_id)
