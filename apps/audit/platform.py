from django.db import transaction

from apps.organizations.platform_models import PlatformAuditEvent


ACTIONS = frozenset({
    "platform.policy_changed", "platform.creation_granted", "platform.creation_revoked",
    "platform.creation_submitted", "platform.creation_withdrawn", "platform.creation_approved",
    "platform.creation_rejected", "platform.admin_granted", "platform.admin_revoked",
})
POLICY_MODES = frozenset({"open", "permission_only", "request_approval"})


def record_platform(*, actor, action, subject_id, metadata=None):
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Platform audit must share the business transaction.")
    if action not in ACTIONS:
        raise ValueError("Unknown platform audit action.")
    fields = {}
    for key, value in (metadata or {}).items():
        if key in {"old_mode", "new_mode"} and isinstance(value, str) and value in POLICY_MODES:
            fields[key] = value
        elif key in {"user_id", "organization_id", "request_id"} and type(value) is int and 0 < value < 2**63:
            fields[key] = value
    return PlatformAuditEvent.objects.create(
        actor=actor, action=action, subject_id=subject_id, metadata=fields,
    )
