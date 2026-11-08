from django.db import migrations


FORWARD_SQL = """
ALTER TABLE departments_department
    ADD CONSTRAINT department_parent_same_organization
    FOREIGN KEY (organization_id, parent_id)
    REFERENCES departments_department (organization_id, id);

CREATE FUNCTION departments_prevent_organization_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.organization_id IS DISTINCT FROM NEW.organization_id THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'department_organization_immutable',
            MESSAGE = 'A department cannot change organizations';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER department_organization_immutable
    BEFORE UPDATE OF organization_id ON departments_department
    FOR EACH ROW EXECUTE FUNCTION departments_prevent_organization_change();

CREATE FUNCTION departments_validate_tree_change() RETURNS trigger
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    changed_ids bigint[];
    tenant_id bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT array_agg(id) INTO changed_ids FROM new_departments;
    ELSE
        SELECT array_agg(new_department.id) INTO changed_ids
        FROM new_departments AS new_department
        LEFT JOIN old_departments AS old_department USING (id)
        WHERE old_department.id IS NULL
           OR new_department.parent_id IS DISTINCT FROM old_department.parent_id
           OR new_department.is_archived IS DISTINCT FROM old_department.is_archived;
    END IF;

    IF changed_ids IS NULL THEN
        RETURN NULL;
    END IF;

    -- Serialize graph changes for each organization. A row version change
    -- also rejects concurrent writers holding an older repeatable-read snapshot.
    FOR tenant_id IN
        SELECT DISTINCT organization_id
        FROM new_departments
        WHERE id = ANY(changed_ids)
        ORDER BY organization_id
    LOOP
        UPDATE organizations_organization
        SET join_policy = join_policy
        WHERE id = tenant_id;
    END LOOP;

    -- Separate queries in this volatile trigger see the committed graph after
    -- the lock wait at READ COMMITTED, plus all changes made by the statement.
    IF EXISTS (
        WITH RECURSIVE ancestors (start_id, organization_id, ancestor_id) AS (
            SELECT id, organization_id, parent_id
            FROM departments_department
            WHERE id = ANY(changed_ids) AND parent_id IS NOT NULL
            UNION
            SELECT walk.start_id, walk.organization_id, parent.parent_id
            FROM ancestors AS walk
            JOIN departments_department AS parent
              ON parent.id = walk.ancestor_id
             AND parent.organization_id = walk.organization_id
            WHERE parent.parent_id IS NOT NULL AND walk.ancestor_id <> walk.start_id
        )
        SELECT FROM ancestors WHERE start_id = ancestor_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'department_tree_no_cycle',
            MESSAGE = 'A department cannot be its own ancestor';
    END IF;

    IF EXISTS (
        SELECT FROM departments_department AS child
        JOIN departments_department AS parent
          ON parent.id = child.parent_id
         AND parent.organization_id = child.organization_id
        WHERE parent.is_archived AND NOT child.is_archived
          AND (child.id = ANY(changed_ids) OR parent.id = ANY(changed_ids))
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'department_tree_active_parent',
            MESSAGE = 'An active department cannot have an archived parent';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER department_tree_check_insert
    AFTER INSERT ON departments_department
    REFERENCING NEW TABLE AS new_departments
    FOR EACH STATEMENT EXECUTE FUNCTION departments_validate_tree_change();

CREATE TRIGGER department_tree_check_update
    AFTER UPDATE ON departments_department
    REFERENCING OLD TABLE AS old_departments NEW TABLE AS new_departments
    FOR EACH STATEMENT EXECUTE FUNCTION departments_validate_tree_change();
"""


REVERSE_SQL = """
DROP TRIGGER department_tree_check_update ON departments_department;
DROP TRIGGER department_tree_check_insert ON departments_department;
DROP TRIGGER department_organization_immutable ON departments_department;
DROP FUNCTION departments_validate_tree_change();
DROP FUNCTION departments_prevent_organization_change();
ALTER TABLE departments_department DROP CONSTRAINT department_parent_same_organization;
"""


class Migration(migrations.Migration):
    dependencies = [("departments", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
