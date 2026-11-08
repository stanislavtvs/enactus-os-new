from django.db import migrations


FORWARD_SQL = """
ALTER TABLE tasks_taskpriority
    ADD CONSTRAINT task_priority_color_hex CHECK (color ~ '^#[0-9A-Fa-f]{6}$');

CREATE FUNCTION tasks_seed_priority_levels(tenant_id bigint) RETURNS void
LANGUAGE sql AS $$
    INSERT INTO tasks_taskpriority (
        organization_id, code, name, rank, sort_order, color,
        is_active, is_default, created_at, updated_at
    )
    SELECT tenant_id, code, name, rank, sort_order, color,
        TRUE, is_default, statement_timestamp(), statement_timestamp()
    FROM (VALUES
        ('low', 'Низкий', 100, 10, '#64748b', FALSE),
        ('normal', 'Обычный', 200, 20, '#2563eb', TRUE),
        ('high', 'Высокий', 300, 30, '#dc2626', FALSE)
    ) AS initial(code, name, rank, sort_order, color, is_default);
$$;

CREATE FUNCTION tasks_seed_new_organization_priorities() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM tasks_seed_priority_levels(NEW.id);
    RETURN NULL;
END;
$$;

CREATE TRIGGER task_priority_seed_organization
    AFTER INSERT ON organizations_organization
    FOR EACH ROW EXECUTE FUNCTION tasks_seed_new_organization_priorities();

-- Installing the organization trigger first also locks out concurrent
-- organization creation until the existing rows have been initialized.
SELECT tasks_seed_priority_levels(id) FROM organizations_organization;

ALTER TABLE tasks_task
    ADD CONSTRAINT task_priority_level_tenant
        FOREIGN KEY (organization_id, priority_level_id)
        REFERENCES tasks_taskpriority (organization_id, id);

UPDATE tasks_task AS task
SET priority_level_id = level.id, priority = level.code
FROM tasks_taskpriority AS level
WHERE level.organization_id = task.organization_id
  AND level.code = CASE WHEN task.priority IN ('low', 'normal', 'high')
                       THEN task.priority ELSE 'normal' END;

SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE tasks_task
    ADD CONSTRAINT task_priority_level_required CHECK (priority_level_id IS NOT NULL);

CREATE FUNCTION tasks_resolve_priority_level() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    resolved_id bigint;
    resolved_code text;
    resolved_active boolean;
    use_legacy boolean := NEW.priority_level_id IS NULL;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.priority_level_id IS NOT DISTINCT FROM OLD.priority_level_id THEN
            IF NEW.priority IS NOT DISTINCT FROM OLD.priority THEN
                RETURN NEW;
            END IF;
            use_legacy := TRUE;
        END IF;
    END IF;

    -- A write to the priority row serializes selection with archival,
    -- including when the other writer has a repeatable-read snapshot.
    IF use_legacy THEN
        UPDATE tasks_taskpriority
        SET is_active = is_active
        WHERE organization_id = NEW.organization_id
          AND (code = NEW.priority OR (NEW.priority IS NULL AND is_default))
        RETURNING id, code, is_active INTO resolved_id, resolved_code, resolved_active;

        IF resolved_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_priority_legacy_code_known',
                MESSAGE = 'The legacy task priority has no configured equivalent';
        END IF;
    ELSE
        UPDATE tasks_taskpriority
        SET is_active = is_active
        WHERE organization_id = NEW.organization_id AND id = NEW.priority_level_id
        RETURNING id, code, is_active INTO resolved_id, resolved_code, resolved_active;

        IF resolved_id IS NULL THEN
            RETURN NEW;  -- Report missing or foreign references through the composite FK.
        END IF;
    END IF;

    IF NOT resolved_active THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_priority_level_active',
            MESSAGE = 'An archived priority cannot be assigned to a task';
    END IF;

    NEW.priority_level_id := resolved_id;
    NEW.priority := CASE WHEN resolved_code IN ('low', 'normal', 'high')
                         THEN resolved_code ELSE 'normal' END;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_priority_level_resolution
    BEFORE INSERT OR UPDATE OF priority, priority_level_id ON tasks_task
    FOR EACH ROW EXECUTE FUNCTION tasks_resolve_priority_level();

CREATE FUNCTION tasks_protect_priority_level() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.organization_id, NEW.code) IS DISTINCT FROM (OLD.organization_id, OLD.code) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_priority_identity_immutable',
            MESSAGE = 'The organization and code of a priority cannot change';
    END IF;

    IF OLD.is_active AND NOT NEW.is_active AND (
        OLD.code IN ('low', 'normal', 'high')
        OR EXISTS (SELECT FROM tasks_task WHERE priority_level_id = OLD.id)
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_priority_archive_unused_only',
            MESSAGE = 'A legacy or referenced priority cannot be archived';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_priority_level_protection
    BEFORE UPDATE OF organization_id, code, is_active ON tasks_taskpriority
    FOR EACH ROW EXECUTE FUNCTION tasks_protect_priority_level();

CREATE FUNCTION tasks_require_priority_default() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tenant_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        tenant_id := OLD.organization_id;
    ELSE
        tenant_id := NEW.organization_id;
    END IF;

    IF NOT EXISTS (SELECT FROM organizations_organization WHERE id = tenant_id) THEN
        RETURN NULL;
    END IF;

    IF NOT EXISTS (
        SELECT FROM tasks_taskpriority WHERE organization_id = tenant_id AND is_default AND is_active
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_priority_active_default_required',
            MESSAGE = 'An organization must retain an active default priority';
    END IF;

    IF TG_OP = 'DELETE' AND OLD.code IN ('low', 'normal', 'high') AND NOT EXISTS (
        SELECT FROM tasks_taskpriority
        WHERE organization_id = tenant_id AND code = OLD.code AND is_active
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_priority_legacy_level_required',
            MESSAGE = 'An organization must retain its legacy priority levels';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER task_priority_default_insert_delete
    AFTER INSERT OR DELETE ON tasks_taskpriority
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION tasks_require_priority_default();

CREATE CONSTRAINT TRIGGER task_priority_default_update
    AFTER UPDATE ON tasks_taskpriority
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (OLD.is_default IS DISTINCT FROM NEW.is_default
                       OR OLD.is_active IS DISTINCT FROM NEW.is_active)
    EXECUTE FUNCTION tasks_require_priority_default();
"""


REVERSE_SQL = """
DROP TRIGGER task_priority_default_update ON tasks_taskpriority;
DROP TRIGGER task_priority_default_insert_delete ON tasks_taskpriority;
DROP FUNCTION tasks_require_priority_default();
DROP TRIGGER task_priority_level_protection ON tasks_taskpriority;
DROP FUNCTION tasks_protect_priority_level();
DROP TRIGGER task_priority_level_resolution ON tasks_task;
DROP FUNCTION tasks_resolve_priority_level();
ALTER TABLE tasks_task
    DROP CONSTRAINT task_priority_level_required,
    DROP CONSTRAINT task_priority_level_tenant;
DROP TRIGGER task_priority_seed_organization ON organizations_organization;
DROP FUNCTION tasks_seed_new_organization_priorities();
DROP FUNCTION tasks_seed_priority_levels(bigint);
ALTER TABLE tasks_taskpriority DROP CONSTRAINT task_priority_color_hex;
"""


class Migration(migrations.Migration):
    dependencies = [("tasks", "0010_task_priorities")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
