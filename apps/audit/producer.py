from .services import record


def snapshot(instance, fields):
    return {field: getattr(instance, field) for field in fields}


def record_change(
    *, user, action, instance, before=None, fields=(), department_id=None,
    project_id=None, metadata=None, force=False,
):
    previous = before or {}
    changes = {
        field: {"old": previous.get(field), "new": getattr(instance, field)}
        for field in fields if previous.get(field) != getattr(instance, field)
    }
    if not changes and not force:
        return
    kind = action.split(".", 1)[0]
    organization_id = instance.pk if kind == "organization" else instance.organization_id
    record(
        organization_id=organization_id, actor_id=user.pk, action=action, object_type=kind,
        object_id=instance.pk, department_id=department_id, project_id=project_id,
        changes=changes, metadata=metadata,
    )


def affected_ids(*ids):
    return sorted({pk for pk in ids if pk is not None})
