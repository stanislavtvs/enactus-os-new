from typing import Literal

from django.conf import settings
from ninja import NinjaAPI, Schema

from apps.access.api import router as access_router
from apps.departments.api import router as departments_router
from apps.departments.membership_api import router as department_membership_router
from apps.organizations.api import router as organizations_router
from apps.organizations.onboarding_api import router as onboarding_router


class HealthResponse(Schema):
    status: Literal["ok"]


api = NinjaAPI(
    title="Enactus OS",
    version="1.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

api.add_router("/organizations", access_router)
api.add_router("/organizations", department_membership_router)
api.add_router("/organizations", departments_router)
api.add_router("/organizations", onboarding_router)
api.add_router("/organizations", organizations_router)


@api.get("/health/", response=HealthResponse, auth=None)
def health(request):
    return {"status": "ok"}
