from typing import Literal

from django.conf import settings
from ninja import NinjaAPI, Schema

from apps.organizations.api import router as organizations_router


class HealthResponse(Schema):
    status: Literal["ok"]


api = NinjaAPI(
    title="Enactus OS",
    version="1.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

api.add_router("/organizations", organizations_router)


@api.get("/health/", response=HealthResponse, auth=None)
def health(request):
    return {"status": "ok"}
