from django.db import migrations


FORWARD_SQL = """
ALTER TABLE tasks_taskstatus
    ADD CONSTRAINT task_workflow_status_color_hex CHECK (color ~ '^#[0-9A-Fa-f]{6}$'),
    ADD CONSTRAINT task_workflow_status_stage_valid
        CHECK (stage IN ('pending', 'in_progress', 'review', 'completed', 'cancelled'));

ALTER TABLE tasks_taskstatustransition
    ADD CONSTRAINT task_transition_source_tenant
        FOREIGN KEY (organization_id, from_status_id)
        REFERENCES tasks_taskstatus (organization_id, id),
    ADD CONSTRAINT task_transition_target_tenant
        FOREIGN KEY (organization_id, to_status_id)
        REFERENCES tasks_taskstatus (organization_id, id),
    ADD CONSTRAINT task_transition_permissions_array
        CHECK (jsonb_typeof(required_permissions) = 'array');

CREATE FUNCTION tasks_seed_workflow_statuses(tenant_id bigint) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO tasks_taskstatus (
        organization_id, code, name, stage, color, sort_order, is_active, is_default,
        created_at, updated_at
    )
    SELECT tenant_id, stage, name, stage, color, sort_order, TRUE, TRUE,
        statement_timestamp(), statement_timestamp()
    FROM (VALUES
        ('pending', 'Ожидает', '#64748b', 10),
        ('in_progress', 'В работе', '#2563eb', 20),
        ('review', 'На проверке', '#7c3aed', 30),
        ('completed', 'Завершена', '#15803d', 40),
        ('cancelled', 'Отменена', '#b91c1c', 50)
    ) AS initial(stage, name, color, sort_order);

    INSERT INTO tasks_taskstatustransition (
        organization_id, from_status_id, to_status_id, is_enabled, allow_assignee,
        required_permissions, created_at, updated_at
    )
    SELECT tenant_id, source.id, target.id, TRUE, FALSE, '[]'::jsonb,
        statement_timestamp(), statement_timestamp()
    FROM (VALUES
        ('pending', 'in_progress'),
        ('pending', 'cancelled'),
        ('in_progress', 'cancelled'),
        ('review', 'cancelled')
    ) AS initial(source_stage, target_stage)
    JOIN tasks_taskstatus AS source
      ON source.organization_id = tenant_id AND source.stage = initial.source_stage
    JOIN tasks_taskstatus AS target
      ON target.organization_id = tenant_id AND target.stage = initial.target_stage;
END;
$$;

CREATE FUNCTION tasks_seed_new_organization_statuses() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM tasks_seed_workflow_statuses(NEW.id);
    RETURN NULL;
END;
$$;

CREATE TRIGGER task_workflow_status_seed_organization
    AFTER INSERT ON organizations_organization
    FOR EACH ROW EXECUTE FUNCTION tasks_seed_new_organization_statuses();

-- The organization trigger installation locks out concurrent organization
-- creation until all existing organizations receive their initial workflow.
SELECT tasks_seed_workflow_statuses(id) FROM organizations_organization;

ALTER TABLE tasks_task
    ADD CONSTRAINT task_workflow_status_scope
        FOREIGN KEY (organization_id, status, workflow_status_id)
        REFERENCES tasks_taskstatus (organization_id, stage, id);

UPDATE tasks_task AS task
SET workflow_status_id = workflow_status.id
FROM tasks_taskstatus AS workflow_status
WHERE workflow_status.organization_id = task.organization_id
  AND workflow_status.stage = task.status AND workflow_status.is_default;

SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE tasks_task
    ADD CONSTRAINT task_workflow_status_required CHECK (workflow_status_id IS NOT NULL);

CREATE FUNCTION tasks_resolve_workflow_status() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    use_default boolean := NEW.workflow_status_id IS NULL;
    resolved_id bigint;
    resolved_active boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.workflow_status_id IS NOT DISTINCT FROM OLD.workflow_status_id THEN
            IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;
            use_default := TRUE;
        END IF;
    END IF;

    IF use_default THEN
        -- Serialize default selection with default configuration changes.
        UPDATE organizations_organization SET join_policy = join_policy WHERE id = NEW.organization_id;

        UPDATE tasks_taskstatus SET is_active = is_active
        WHERE organization_id = NEW.organization_id AND stage = NEW.status AND is_default
        RETURNING id, is_active INTO resolved_id, resolved_active;

        IF resolved_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_workflow_status_default_required',
                MESSAGE = 'The task stage requires an active default workflow status';
        END IF;
    ELSE
        -- Writing the selected row serializes this check with archival, even
        -- when either transaction is using a repeatable-read snapshot.
        UPDATE tasks_taskstatus SET is_active = is_active
        WHERE organization_id = NEW.organization_id AND stage = NEW.status AND id = NEW.workflow_status_id
        RETURNING id, is_active INTO resolved_id, resolved_active;

        IF resolved_id IS NULL THEN
            RETURN NEW;  -- The composite FK reports a missing or mismatched reference.
        END IF;
    END IF;

    IF NOT resolved_active THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_workflow_status_active',
            MESSAGE = 'An archived workflow status cannot be selected for a task';
    END IF;

    NEW.workflow_status_id := resolved_id;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_workflow_status_resolution
    BEFORE INSERT OR UPDATE OF status, workflow_status_id ON tasks_task
    FOR EACH ROW EXECUTE FUNCTION tasks_resolve_workflow_status();

CREATE FUNCTION tasks_protect_workflow_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.organization_id, NEW.code, NEW.stage)
        IS DISTINCT FROM (OLD.organization_id, OLD.code, OLD.stage)
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_workflow_status_identity_immutable',
            MESSAGE = 'The organization, code and stage of a workflow status cannot change';
    END IF;

    IF NEW.is_default IS DISTINCT FROM OLD.is_default
        OR (OLD.is_default AND NEW.is_active IS DISTINCT FROM OLD.is_active)
    THEN
        UPDATE organizations_organization SET join_policy = join_policy WHERE id = NEW.organization_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_workflow_status_protection
    BEFORE UPDATE OF organization_id, code, stage, is_default, is_active ON tasks_taskstatus
    FOR EACH ROW EXECUTE FUNCTION tasks_protect_workflow_status();

CREATE FUNCTION tasks_require_workflow_default() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tenant_id bigint;
    affected_stage text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        tenant_id := OLD.organization_id;
        affected_stage := OLD.stage;
    ELSE
        tenant_id := NEW.organization_id;
        affected_stage := NEW.stage;
    END IF;

    IF EXISTS (SELECT FROM organizations_organization WHERE id = tenant_id)
        AND NOT EXISTS (
            SELECT FROM tasks_taskstatus
            WHERE organization_id = tenant_id AND stage = affected_stage AND is_default AND is_active
        )
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_workflow_stage_default_required',
            MESSAGE = 'Each task stage must retain an active default workflow status';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER task_workflow_status_default_insert_delete
    AFTER INSERT OR DELETE ON tasks_taskstatus
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION tasks_require_workflow_default();

CREATE CONSTRAINT TRIGGER task_workflow_status_default_update
    AFTER UPDATE ON tasks_taskstatus
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (OLD.is_default IS DISTINCT FROM NEW.is_default
                       OR (OLD.is_default AND OLD.is_active IS DISTINCT FROM NEW.is_active))
    EXECUTE FUNCTION tasks_require_workflow_default();

CREATE FUNCTION tasks_validate_manual_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    source_stage text;
    target_stage text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_transition_organization_immutable',
            MESSAGE = 'A workflow transition cannot change organizations';
    END IF;

    SELECT stage INTO source_stage FROM tasks_taskstatus
    WHERE organization_id = NEW.organization_id AND id = NEW.from_status_id;
    SELECT stage INTO target_stage FROM tasks_taskstatus
    WHERE organization_id = NEW.organization_id AND id = NEW.to_status_id;

    IF source_stage <> target_stage AND NOT (
        (source_stage = 'pending' AND target_stage = 'in_progress')
        OR (source_stage IN ('pending', 'in_progress', 'review') AND target_stage = 'cancelled')
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_transition_manual_stage_pair',
            MESSAGE = 'This cross-stage transition is reserved for system workflow';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_transition_manual_stage_guard
    BEFORE INSERT OR UPDATE OF organization_id, from_status_id, to_status_id ON tasks_taskstatustransition
    FOR EACH ROW EXECUTE FUNCTION tasks_validate_manual_transition();
"""


REVERSE_SQL = """
DROP TRIGGER task_transition_manual_stage_guard ON tasks_taskstatustransition;
DROP FUNCTION tasks_validate_manual_transition();
DROP TRIGGER task_workflow_status_default_update ON tasks_taskstatus;
DROP TRIGGER task_workflow_status_default_insert_delete ON tasks_taskstatus;
DROP FUNCTION tasks_require_workflow_default();
DROP TRIGGER task_workflow_status_protection ON tasks_taskstatus;
DROP FUNCTION tasks_protect_workflow_status();
DROP TRIGGER task_workflow_status_resolution ON tasks_task;
DROP FUNCTION tasks_resolve_workflow_status();
ALTER TABLE tasks_task
    DROP CONSTRAINT task_workflow_status_required,
    DROP CONSTRAINT task_workflow_status_scope;
DROP TRIGGER task_workflow_status_seed_organization ON organizations_organization;
DROP FUNCTION tasks_seed_new_organization_statuses();
DROP FUNCTION tasks_seed_workflow_statuses(bigint);
ALTER TABLE tasks_taskstatustransition
    DROP CONSTRAINT task_transition_permissions_array,
    DROP CONSTRAINT task_transition_target_tenant,
    DROP CONSTRAINT task_transition_source_tenant;
ALTER TABLE tasks_taskstatus
    DROP CONSTRAINT task_workflow_status_stage_valid,
    DROP CONSTRAINT task_workflow_status_color_hex;
"""


class Migration(migrations.Migration):
    dependencies = [("tasks", "0012_task_workflow_statuses")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
