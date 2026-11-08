from django.db import migrations


FORWARD_SQL = """
ALTER TABLE tasks_taskdependency
    ADD CONSTRAINT task_dependency_task_tenant
        FOREIGN KEY (organization_id, task_id)
        REFERENCES tasks_task (organization_id, id),
    ADD CONSTRAINT task_dependency_predecessor_tenant
        FOREIGN KEY (organization_id, predecessor_id)
        REFERENCES tasks_task (organization_id, id),
    ADD CONSTRAINT task_dependency_kind_valid CHECK (kind IN ('informational', 'strict'));

CREATE FUNCTION tasks_prevent_dependency_reassignment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'check_violation',
        CONSTRAINT = 'task_dependency_organization_immutable',
        MESSAGE = 'A task dependency cannot change organizations';
END;
$$;

CREATE TRIGGER task_dependency_organization_immutable
    BEFORE UPDATE OF organization_id ON tasks_taskdependency
    FOR EACH ROW WHEN (OLD.organization_id IS DISTINCT FROM NEW.organization_id)
    EXECUTE FUNCTION tasks_prevent_dependency_reassignment();

CREATE FUNCTION tasks_validate_dependency_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tenants bigint[];
    tenant_id bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT array_agg(DISTINCT organization_id ORDER BY organization_id)
        INTO tenants FROM old_dependencies;
    ELSE
        SELECT array_agg(DISTINCT organization_id ORDER BY organization_id)
        INTO tenants FROM new_dependencies;
    END IF;

    IF tenants IS NULL THEN
        RETURN NULL;
    END IF;

    -- Use the same versioned organization row as the task start trigger,
    -- including for edge deletion and changes between dependency kinds.
    FOREACH tenant_id IN ARRAY tenants LOOP
        UPDATE organizations_organization SET join_policy = join_policy WHERE id = tenant_id;
    END LOOP;

    IF TG_OP = 'DELETE' THEN
        RETURN NULL;
    END IF;

    IF EXISTS (
        WITH RECURSIVE walk (start_id, organization_id, node_id) AS (
            SELECT task_id, organization_id, predecessor_id FROM new_dependencies
            UNION
            SELECT walk.start_id, walk.organization_id, edge.predecessor_id
            FROM walk
            JOIN tasks_taskdependency AS edge
              ON edge.organization_id = walk.organization_id AND edge.task_id = walk.node_id
            WHERE walk.node_id <> walk.start_id
        )
        SELECT FROM walk WHERE node_id = start_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_dependency_graph_no_cycle',
            MESSAGE = 'A task dependency cannot create a cycle';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER task_dependency_check_insert
    AFTER INSERT ON tasks_taskdependency
    REFERENCING NEW TABLE AS new_dependencies
    FOR EACH STATEMENT EXECUTE FUNCTION tasks_validate_dependency_change();

CREATE TRIGGER task_dependency_check_update
    AFTER UPDATE ON tasks_taskdependency
    REFERENCING NEW TABLE AS new_dependencies
    FOR EACH STATEMENT EXECUTE FUNCTION tasks_validate_dependency_change();

CREATE TRIGGER task_dependency_lock_delete
    AFTER DELETE ON tasks_taskdependency
    REFERENCING OLD TABLE AS old_dependencies
    FOR EACH STATEMENT EXECUTE FUNCTION tasks_validate_dependency_change();

CREATE FUNCTION tasks_check_strict_dependency_start() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Lock even when the task has no edges yet. At READ COMMITTED the
    -- following query sees committed inserts after a lock wait; at
    -- REPEATABLE READ a stale transaction cannot write this row version.
    UPDATE organizations_organization SET join_policy = join_policy WHERE id = NEW.organization_id;

    IF EXISTS (
        SELECT FROM tasks_taskdependency AS edge
        JOIN tasks_task AS predecessor
          ON predecessor.organization_id = edge.organization_id AND predecessor.id = edge.predecessor_id
        WHERE edge.organization_id = NEW.organization_id AND edge.task_id = NEW.id
          AND edge.kind = 'strict' AND predecessor.status <> 'completed'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_strict_dependencies_block_start',
            MESSAGE = 'A strict predecessor must complete before this task can start';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_dependency_start_guard
    BEFORE UPDATE OF status ON tasks_task
    FOR EACH ROW WHEN (OLD.status = 'pending' AND NEW.status IN ('in_progress', 'review'))
    EXECUTE FUNCTION tasks_check_strict_dependency_start();
"""


REVERSE_SQL = """
DROP TRIGGER task_dependency_start_guard ON tasks_task;
DROP FUNCTION tasks_check_strict_dependency_start();
DROP TRIGGER task_dependency_lock_delete ON tasks_taskdependency;
DROP TRIGGER task_dependency_check_update ON tasks_taskdependency;
DROP TRIGGER task_dependency_check_insert ON tasks_taskdependency;
DROP FUNCTION tasks_validate_dependency_change();
DROP TRIGGER task_dependency_organization_immutable ON tasks_taskdependency;
DROP FUNCTION tasks_prevent_dependency_reassignment();
ALTER TABLE tasks_taskdependency
    DROP CONSTRAINT task_dependency_kind_valid,
    DROP CONSTRAINT task_dependency_predecessor_tenant,
    DROP CONSTRAINT task_dependency_task_tenant;
"""


class Migration(migrations.Migration):
    dependencies = [("tasks", "0008_task_dependencies")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
