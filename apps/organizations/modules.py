from dataclasses import dataclass
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.access.services import organization_access
from apps.audit.context import current_operation_id
from apps.audit.services import record

MODULES_MANAGE = "organizations.modules.manage"
PROJECTS = "projects"
STAFFING = "staffing"
TASKS = "tasks"
CALENDAR = "calendar"


@dataclass(frozen=True)
class Module:
    name: str
    description: str
    required: bool = False
    dependencies: tuple[str, ...] = ()


CATALOG = {
    "core": Module(
        "Ядро организации", "Доступ, участники, подразделения, роли и настройки организации.", required=True,
    ),
    "audit": Module("Журнал действий", "Запись истории критических изменений.", required=True),
    "notifications": Module(
        "Уведомления", "Внутренние уведомления и обязательные сообщения рабочих процессов.", required=True,
    ),
    PROJECTS: Module(
        "Проекты", "Проекты, команды и участие подразделений. При отключении скрываются связанные задачи и события.",
    ),
    STAFFING: Module(
        "Запросы специалистов", "Запросы проектов на участие сотрудников подразделений.", dependencies=(PROJECTS,),
    ),
    TASKS: Module("Задачи", "Задачи, распределение, проверки и отображение дедлайнов в календаре."),
    CALENDAR: Module("Календарь", "Встречи, мероприятия, повторения и приглашения."),
}


class ModuleDisabled(Exception):
    code = "module_disabled"

    def __init__(self, module_code):
        self.module_code = module_code
        super().__init__("Модуль отключён в этой организации.")


class ModuleConfigurationError(Exception):
    def __init__(self, code, detail, module_code, *, dependency=None):
        self.code = code
        self.module_code = module_code
        self.dependency = dependency
        super().__init__(detail)


def enabled(organization, code):
    module = CATALOG.get(code)
    return module is not None and (module.required or code not in organization.disabled_modules)


def require_module(organization, code):
    if not enabled(organization, code):
        raise ModuleDisabled(code)


def _catalog(access):
    return {
        "can_manage": access.allows(MODULES_MANAGE),
        "items": [
            {
                "code": code, "name": module.name, "description": module.description,
                "enabled": enabled(access.organization, code), "required": module.required,
                "configurable": not module.required, "dependencies": list(module.dependencies),
            }
            for code, module in CATALOG.items()
        ],
    }


def list_modules(*, user, org_id):
    return _catalog(organization_access(user=user, org_id=org_id))


@transaction.atomic
def set_modules(*, user, org_id, changes):
    access = organization_access(user=user, org_id=org_id, lock=True)
    if not access.allows(MODULES_MANAGE):
        raise PermissionDenied
    organization = access.organization
    previous = set(organization.disabled_modules)
    disabled = previous.copy()
    for code, state in changes.items():
        module = CATALOG.get(code)
        if module is None:
            raise ModuleConfigurationError("unknown_module", "Неизвестный модуль.", code)
        if type(state) is not bool:
            raise ModuleConfigurationError("invalid_module_state", "Укажите состояние модуля.", code)
        if module.required and not state:
            raise ModuleConfigurationError("required_module", "Обязательный модуль нельзя отключить.", code)
        if state:
            disabled.discard(code)
        else:
            disabled.add(code)

    for code, module in CATALOG.items():
        if code not in disabled:
            for dependency in module.dependencies:
                if dependency in disabled:
                    raise ModuleConfigurationError(
                        "module_dependency",
                        f"Модуль «{module.name}» требует включённый модуль «{CATALOG[dependency].name}».",
                        code, dependency=dependency,
                    )
    if previous == disabled:
        return _catalog(access)

    organization.disabled_modules = sorted(disabled)
    organization.save(update_fields=["disabled_modules"])
    operation_id = current_operation_id.get() or uuid4()
    for code in sorted(previous ^ disabled):
        record(
            organization_id=org_id, actor_id=user.pk, action="organization.module_changed",
            object_type="organization", object_id=org_id, operation_id=operation_id,
            metadata={"code": code},
            changes={"is_enabled": {"old": code not in previous, "new": code not in disabled}},
        )
    return _catalog(access)
