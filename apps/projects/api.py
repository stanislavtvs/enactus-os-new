from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from ninja import PatchDict, Router
from ninja.errors import HttpError
from ninja.security import django_auth

from apps.access.services import organization_access

from . import services
from .schemas import (
    LeadInput,
    ProjectChangeInput,
    ProjectInput,
    ProjectOptions,
    ProjectOutput,
    RecordId,
    ResponsibleDepartmentInput,
)

router = Router(auth=django_auth)
PROJECT_CONFLICT_CONSTRAINTS = {
    "project_requires_active_department",
    "project_requires_active_lead",
}


def _perform(operation, **kwargs):
    try:
        return operation(**kwargs)
    except PermissionDenied:
        raise HttpError(403, "Forbidden") from None
    except services.ProjectConflict as exc:
        raise HttpError(409, str(exc)) from None
    except services.ProjectValidation as exc:
        raise HttpError(422, str(exc)) from None
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
        if constraint not in PROJECT_CONFLICT_CONSTRAINTS:
            raise
        raise HttpError(409, "Связанное подразделение или членство стало неактивным.") from None


def _project_data(project, access):
    lead = project.lead_membership
    return {
        "id": project.pk,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "start_date": project.start_date,
        "planned_end_date": project.planned_end_date,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "is_archived": project.is_archived,
        "lead": None if lead is None else {"id": lead.pk, "username": lead.user.get_username()},
        "responsible_department": project.responsible_department,
        "participating_departments": [
            link.department
            for link in project.department_links.all()
        ],
        "permissions": services.project_permissions(access=access, project=project),
    }


@transaction.atomic
def _changed_project(request, org_id, operation, **kwargs):
    project = _perform(operation, user=request.user, org_id=org_id, **kwargs)
    access = organization_access(user=request.user, org_id=org_id)
    project = services.visible_project(access=access, project_id=project.pk)
    return _project_data(project, access)


@router.get("/{org_id}/projects/", response=list[ProjectOutput], url_name="project_list")
def list_projects(request, org_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    return [_project_data(project, access) for project in services.visible_projects(access=access)]


@router.post("/{org_id}/projects/", response={201: ProjectOutput})
def create_project(request, org_id: RecordId, payload: ProjectInput):
    return 201, _changed_project(request, org_id, services.create_project, **payload.model_dump())


@router.get("/{org_id}/projects/{project_id}/", response=ProjectOutput, url_name="project_detail")
def get_project(request, org_id: RecordId, project_id: RecordId):
    access = organization_access(user=request.user, org_id=org_id)
    return _project_data(services.visible_project(access=access, project_id=project_id), access)


@router.patch("/{org_id}/projects/{project_id}/", response=ProjectOutput)
def update_project(request, org_id: RecordId, project_id: RecordId, payload: PatchDict[ProjectChangeInput]):
    return _changed_project(request, org_id, services.update_project, project_id=project_id, changes=payload)


@router.delete("/{org_id}/projects/{project_id}/", response={204: None})
def archive_project(request, org_id: RecordId, project_id: RecordId):
    _perform(services.archive_project, user=request.user, org_id=org_id, project_id=project_id)
    return 204, None


@router.put("/{org_id}/projects/{project_id}/lead/", response=ProjectOutput)
def set_project_lead(request, org_id: RecordId, project_id: RecordId, payload: LeadInput):
    return _changed_project(
        request, org_id, services.set_project_lead, project_id=project_id,
        lead_membership_id=payload.lead_membership_id,
    )


@router.put("/{org_id}/projects/{project_id}/responsible-department/", response=ProjectOutput)
def set_responsible_department(request, org_id: RecordId, project_id: RecordId, payload: ResponsibleDepartmentInput):
    return _changed_project(
        request, org_id, services.set_responsible_department, project_id=project_id,
        responsible_department_id=payload.responsible_department_id,
    )


@router.put("/{org_id}/projects/{project_id}/departments/{department_id}/", response={204: None})
def add_participating_department(request, org_id: RecordId, project_id: RecordId, department_id: RecordId):
    _perform(
        services.set_participating_department,
        user=request.user, org_id=org_id, project_id=project_id, department_id=department_id, include=True,
    )
    return 204, None


@router.delete("/{org_id}/projects/{project_id}/departments/{department_id}/", response={204: None})
def remove_participating_department(request, org_id: RecordId, project_id: RecordId, department_id: RecordId):
    _perform(
        services.set_participating_department,
        user=request.user, org_id=org_id, project_id=project_id, department_id=department_id, include=False,
    )
    return 204, None


@router.get("/{org_id}/project-options/", response=ProjectOptions, url_name="project_options")
def get_project_options(request, org_id: RecordId, project_id: RecordId | None = None):
    access = organization_access(user=request.user, org_id=org_id)
    return services.project_options(access=access, project_id=project_id)
