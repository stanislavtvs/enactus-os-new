from django.db import migrations


FORWARD_SQL = """
ALTER TABLE tasks_task
    ADD CONSTRAINT task_parent_tenant
    FOREIGN KEY (organization_id, parent_id)
    REFERENCES tasks_task (organization_id, id);

CREATE FUNCTION tasks_validate_parent_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    changed_ids bigint[];
    affected_organizations bigint[];
    tenant_id bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT array_agg(id), array_agg(DISTINCT organization_id)
        INTO changed_ids, affected_organizations
        FROM new_tasks WHERE parent_id IS NOT NULL;
    ELSE
        SELECT array_agg(new_task.id) FILTER (
            WHERE old_task.id IS NULL OR new_task.parent_id IS DISTINCT FROM old_task.parent_id
        ), array_agg(DISTINCT new_task.organization_id)
        INTO changed_ids, affected_organizations
        FROM new_tasks AS new_task
        LEFT JOIN old_tasks AS old_task USING (id)
        WHERE old_task.id IS NULL OR new_task.parent_id IS DISTINCT FROM old_task.parent_id
           OR new_task.is_archived IS DISTINCT FROM old_task.is_archived;
    END IF;

    -- Serialize tree edits and parent archives, including at REPEATABLE READ.
    FOR tenant_id IN
        SELECT organization_id FROM unnest(affected_organizations) AS affected(organization_id)
        ORDER BY organization_id
    LOOP
        UPDATE organizations_organization SET join_policy = join_policy WHERE id = tenant_id;
    END LOOP;

    IF changed_ids IS NULL THEN
        RETURN NULL;
    END IF;

    IF EXISTS (
        SELECT FROM tasks_task AS child
        JOIN tasks_task AS parent
          ON parent.organization_id = child.organization_id AND parent.id = child.parent_id
        WHERE child.id = ANY(changed_ids) AND parent.is_archived
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_new_parent_must_be_active',
            MESSAGE = 'An archived task cannot be selected as a new parent';
    END IF;

    IF EXISTS (
        WITH RECURSIVE ancestors (start_id, organization_id, ancestor_id) AS (
            SELECT id, organization_id, parent_id FROM tasks_task
            WHERE id = ANY(changed_ids) AND parent_id IS NOT NULL
            UNION
            SELECT walk.start_id, walk.organization_id, parent.parent_id
            FROM ancestors AS walk
            JOIN tasks_task AS parent
              ON parent.organization_id = walk.organization_id AND parent.id = walk.ancestor_id
            WHERE parent.parent_id IS NOT NULL AND walk.ancestor_id <> walk.start_id
        )
        SELECT FROM ancestors WHERE start_id = ancestor_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_parent_tree_no_cycle',
            MESSAGE = 'A task cannot be its own ancestor';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER task_parent_tree_check_insert
    AFTER INSERT ON tasks_task
    REFERENCING NEW TABLE AS new_tasks
    FOR EACH STATEMENT EXECUTE FUNCTION tasks_validate_parent_change();

CREATE TRIGGER task_parent_tree_check_update
    AFTER UPDATE ON tasks_task
    REFERENCING OLD TABLE AS old_tasks NEW TABLE AS new_tasks
    FOR EACH STATEMENT EXECUTE FUNCTION tasks_validate_parent_change();

ALTER TABLE tasks_checklist
    ADD CONSTRAINT checklist_task_tenant
        FOREIGN KEY (organization_id, task_id)
        REFERENCES tasks_task (organization_id, id);

ALTER TABLE tasks_checklistitem
    ADD CONSTRAINT checklist_item_checklist_scope
        FOREIGN KEY (organization_id, task_id, checklist_id)
        REFERENCES tasks_checklist (organization_id, task_id, id),
    ADD CONSTRAINT checklist_item_completed_by_tenant
        FOREIGN KEY (organization_id, completed_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

ALTER TABLE tasks_checklistconversion
    ADD CONSTRAINT checklist_conversion_parent_tenant
        FOREIGN KEY (organization_id, parent_task_id)
        REFERENCES tasks_task (organization_id, id),
    ADD CONSTRAINT checklist_conversion_task_tenant
        FOREIGN KEY (organization_id, task_id)
        REFERENCES tasks_task (organization_id, id),
    ADD CONSTRAINT checklist_conversion_item_scope
        FOREIGN KEY (organization_id, parent_task_id, item_id)
        REFERENCES tasks_checklistitem (organization_id, task_id, id),
    ADD CONSTRAINT checklist_conversion_actor_tenant
        FOREIGN KEY (organization_id, actor_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

CREATE FUNCTION tasks_validate_checklist_conversion() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    current_parent bigint;
    task_status text;
    task_archived boolean;
    item_archived_at timestamptz;
BEGIN
    -- The history records the parent at conversion time, while later
    -- legitimate moves of the subtask remain possible.
    UPDATE organizations_organization SET join_policy = join_policy WHERE id = NEW.organization_id;

    UPDATE tasks_task SET is_archived = is_archived
    WHERE organization_id = NEW.organization_id AND id = NEW.task_id
    RETURNING parent_id, status, is_archived INTO current_parent, task_status, task_archived;

    IF FOUND AND current_parent IS DISTINCT FROM NEW.parent_task_id THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'checklist_conversion_task_parent_matches',
            MESSAGE = 'A conversion must record the current parent task';
    END IF;
    UPDATE tasks_checklistitem SET archived_at = archived_at
    WHERE organization_id = NEW.organization_id AND task_id = NEW.parent_task_id AND id = NEW.item_id
    RETURNING archived_at INTO item_archived_at;

    IF FOUND AND task_status IS NOT NULL AND (
        CASE NEW.direction
            WHEN 'item_to_task' THEN NOT task_archived AND task_status = 'pending'
                AND item_archived_at IS NOT NULL
            WHEN 'task_to_item' THEN task_archived AND item_archived_at IS NULL
            ELSE FALSE
        END
    ) IS NOT TRUE THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'checklist_conversion_source_archived',
            MESSAGE = 'A conversion must preserve an archived source and create an active target';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER checklist_conversion_parent_matches
    BEFORE INSERT ON tasks_checklistconversion
    FOR EACH ROW EXECUTE FUNCTION tasks_validate_checklist_conversion();

CREATE FUNCTION tasks_protect_checklist_conversion_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'check_violation',
        CONSTRAINT = 'checklist_conversion_history_append_only',
        MESSAGE = 'Checklist conversion history cannot be changed or deleted';
END;
$$;

CREATE TRIGGER checklist_conversion_history_append_only
    BEFORE UPDATE OR DELETE ON tasks_checklistconversion
    FOR EACH ROW EXECUTE FUNCTION tasks_protect_checklist_conversion_history();
"""


REVERSE_SQL = """
DROP TRIGGER checklist_conversion_history_append_only ON tasks_checklistconversion;
DROP FUNCTION tasks_protect_checklist_conversion_history();
DROP TRIGGER checklist_conversion_parent_matches ON tasks_checklistconversion;
DROP FUNCTION tasks_validate_checklist_conversion();

ALTER TABLE tasks_checklistconversion
    DROP CONSTRAINT checklist_conversion_actor_tenant,
    DROP CONSTRAINT checklist_conversion_item_scope,
    DROP CONSTRAINT checklist_conversion_task_tenant,
    DROP CONSTRAINT checklist_conversion_parent_tenant;
ALTER TABLE tasks_checklistitem
    DROP CONSTRAINT checklist_item_completed_by_tenant,
    DROP CONSTRAINT checklist_item_checklist_scope;
ALTER TABLE tasks_checklist DROP CONSTRAINT checklist_task_tenant;

DROP TRIGGER task_parent_tree_check_update ON tasks_task;
DROP TRIGGER task_parent_tree_check_insert ON tasks_task;
DROP FUNCTION tasks_validate_parent_change();
ALTER TABLE tasks_task DROP CONSTRAINT task_parent_tenant;
"""


class Migration(migrations.Migration):
    dependencies = [("tasks", "0006_task_decomposition")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
