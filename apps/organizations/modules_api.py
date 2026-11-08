from typing import Annotated

from django.http import HttpResponse
from ninja import Router, Schema
from ninja.security import django_auth
from pydantic import Field, StrictBool

from . import modules

router = Router(auth=django_auth)
RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
ModuleCode = Annotated[str, Field(min_length=1, max_length=24, pattern=r"^[a-z_]+$")]


class ModuleItem(Schema):
    code: str
    name: str
    description: str
    enabled: bool
    required: bool
    configurable: bool
    dependencies: list[str]


class ModuleCatalog(Schema):
    can_manage: bool
    items: list[ModuleItem]


class ModuleChanges(Schema):
    changes: Annotated[dict[ModuleCode, StrictBool], Field(max_length=len(modules.CATALOG))]


class ModuleConflict(Schema):
    code: str
    detail: str
    module: str
    dependency: str | None = None


@router.get("/{org_id}/modules/", response=ModuleCatalog, url_name="organization_modules")
def catalog(request, response: HttpResponse, org_id: RecordId):
    response["Cache-Control"] = "no-store"
    return modules.list_modules(user=request.user, org_id=org_id)


@router.patch("/{org_id}/modules/", response={200: ModuleCatalog, 409: ModuleConflict, 422: ModuleConflict})
def configure(request, response: HttpResponse, org_id: RecordId, payload: ModuleChanges):
    response["Cache-Control"] = "no-store"
    try:
        result = modules.set_modules(user=request.user, org_id=org_id, changes=payload.changes)
    except modules.ModuleConfigurationError as exc:
        status = 422 if exc.code in {"unknown_module", "invalid_module_state"} else 409
        return status, {
            "code": exc.code, "detail": str(exc), "module": exc.module_code, "dependency": exc.dependency,
        }
    return 200, result
