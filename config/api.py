from typing import Literal

from django.conf import settings
from ninja import NinjaAPI, Schema

from apps.tasks.api import router as tasks_router

from apps.access.api import router as access_router
from apps.calendar_app.api import router as calendar_router
from apps.calendar_app.invitation_api import router as calendar_invitation_router
from apps.calendar_app.recurrence_api import router as calendar_recurrence_router
from apps.audit.api import router as audit_router
from apps.notifications.api import router as notifications_router
from apps.notifications.policy_api import router as notification_policy_router
from apps.departments.api import router as departments_router
from apps.departments.membership_api import router as department_membership_router
from apps.organizations.api import router as organizations_router
from apps.projects.api import router as projects_router
from apps.projects.staffing_api import router as staffing_router
from apps.organizations.onboarding_api import router as onboarding_router


class HealthResponse(Schema):
    status: Literal["ok"]


api = NinjaAPI(
    title="Enactus OS",
    version="1.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

api.add_router("", notifications_router)
api.add_router("/organizations", audit_router)
api.add_router("/organizations", notification_policy_router)
api.add_router("/organizations", calendar_router)
api.add_router("/organizations", calendar_invitation_router)
api.add_router("/organizations", calendar_recurrence_router)
api.add_router("/organizations", tasks_router)
api.add_router("/organizations", staffing_router)
api.add_router("/organizations", projects_router)
api.add_router("/organizations", access_router)
api.add_router("/organizations", department_membership_router)
api.add_router("/organizations", departments_router)
api.add_router("/organizations", onboarding_router)
api.add_router("/organizations", organizations_router)


@api.get("/health/", response=HealthResponse, auth=None)
def health(request):
    return {"status": "ok"}
