from django.db import migrations


FORWARD_SQL = """
ALTER TABLE tasks_task
    ADD CONSTRAINT task_author_tenant
        FOREIGN KEY (organization_id, created_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT task_assignee_tenant
        FOREIGN KEY (organization_id, assignee_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT task_project_tenant
        FOREIGN KEY (organization_id, project_id)
        REFERENCES projects_project (organization_id, id),
    ADD CONSTRAINT task_department_tenant
        FOREIGN KEY (organization_id, department_id)
        REFERENCES departments_department (organization_id, id);

ALTER TABLE tasks_taskcollaborator
    ADD CONSTRAINT task_collaborator_task_tenant
        FOREIGN KEY (organization_id, task_id) REFERENCES tasks_task (organization_id, id),
    ADD CONSTRAINT task_collaborator_member_tenant
        FOREIGN KEY (organization_id, organization_membership_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

CREATE FUNCTION tasks_require_eligible_participant(
    tenant_id bigint, member_id bigint, target_project_id bigint
) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    org_member_active boolean;
    project_member_active boolean;
BEGIN
    -- New row versions serialize with deactivation and protect an older
    -- repeatable-read snapshot from missing the associated task changes.
    UPDATE organizations_organizationmembership
    SET is_active = is_active
    WHERE organization_id = tenant_id AND id = member_id
    RETURNING is_active INTO org_member_active;

    IF org_member_active IS FALSE THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_participant_requires_active_org_member',
            MESSAGE = 'A task participant requires an active organization membership';
    END IF;

    IF target_project_id IS NOT NULL THEN
        UPDATE projects_projectmembership
        SET is_active = is_active
        WHERE organization_id = tenant_id AND project_id = target_project_id
          AND organization_membership_id = member_id
        RETURNING is_active INTO project_member_active;

        IF project_member_active IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_participant_requires_active_project_member',
                MESSAGE = 'A project task participant requires an active project membership';
        END IF;
    END IF;
END;
$$;

CREATE FUNCTION tasks_validate_references() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    assignee_changed boolean := TRUE;
    reopening boolean := FALSE;
    collaborator_id bigint;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.organization_id, NEW.project_id, NEW.created_by_id)
            IS DISTINCT FROM (OLD.organization_id, OLD.project_id, OLD.created_by_id)
        THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_identity_immutable',
                MESSAGE = 'A task cannot change its organization, project or author';
        END IF;
        assignee_changed := NEW.assignee_id IS DISTINCT FROM OLD.assignee_id;
        reopening := NOT NEW.is_archived AND NEW.status NOT IN ('completed', 'cancelled')
            AND (OLD.is_archived OR OLD.status IN ('completed', 'cancelled'));
    END IF;

    IF NEW.assignee_id IS NOT NULL AND (assignee_changed OR reopening) THEN
        PERFORM tasks_require_eligible_participant(
            NEW.organization_id, NEW.assignee_id, NEW.project_id
        );

        -- A collaborator activation writes the same task row before checking
        -- the primary assignee. This query sees its committed changes.
        IF EXISTS (
            SELECT FROM tasks_taskcollaborator
            WHERE organization_id = NEW.organization_id AND task_id = NEW.id
              AND organization_membership_id = NEW.assignee_id AND is_active
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_participant_roles_distinct',
                MESSAGE = 'A task assignee cannot be an active collaborator on the same task';
        END IF;
    END IF;

    IF reopening THEN
        FOR collaborator_id IN
            SELECT organization_membership_id FROM tasks_taskcollaborator
            WHERE organization_id = NEW.organization_id AND task_id = NEW.id AND is_active
            ORDER BY organization_membership_id
        LOOP
            PERFORM tasks_require_eligible_participant(
                NEW.organization_id, collaborator_id, NEW.project_id
            );
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_reference_guard
    BEFORE INSERT OR UPDATE OF organization_id, project_id, created_by_id, assignee_id,
        status, is_archived ON tasks_task
    FOR EACH ROW EXECUTE FUNCTION tasks_validate_references();

CREATE FUNCTION tasks_validate_collaborator() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    task_project bigint;
    task_assignee bigint;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.organization_id, NEW.task_id, NEW.organization_membership_id)
            IS DISTINCT FROM (OLD.organization_id, OLD.task_id, OLD.organization_membership_id)
        THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_collaborator_identity_immutable',
                MESSAGE = 'A task collaborator cannot change its task or participant';
        END IF;
        IF NEW.is_active IS NOT DISTINCT FROM OLD.is_active THEN
            RETURN NEW;
        END IF;
    END IF;

    IF NEW.is_active THEN
        UPDATE tasks_task
        SET assignee_id = assignee_id
        WHERE organization_id = NEW.organization_id AND id = NEW.task_id
        RETURNING project_id, assignee_id INTO task_project, task_assignee;

        IF task_assignee = NEW.organization_membership_id THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_participant_roles_distinct',
                MESSAGE = 'A task assignee cannot be an active collaborator on the same task';
        END IF;

        PERFORM tasks_require_eligible_participant(
            NEW.organization_id, NEW.organization_membership_id, task_project
        );
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_collaborator_reference_guard
    BEFORE INSERT OR UPDATE OF organization_id, task_id, organization_membership_id, is_active
    ON tasks_taskcollaborator
    FOR EACH ROW EXECUTE FUNCTION tasks_validate_collaborator();

CREATE FUNCTION tasks_close_membership_assignments() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_member_id bigint;
    target_project_id bigint;
BEGIN
    IF TG_ARGV[0] = 'project' THEN
        target_member_id := NEW.organization_membership_id;
        target_project_id := NEW.project_id;
    ELSE
        target_member_id := NEW.id;
    END IF;

    UPDATE tasks_task
    SET assignee_id = NULL,
        status = CASE WHEN status IN ('in_progress', 'review') THEN 'pending' ELSE status END,
        updated_at = statement_timestamp()
    WHERE organization_id = NEW.organization_id
      AND assignee_id = target_member_id
      AND (target_project_id IS NULL OR project_id = target_project_id)
      AND NOT is_archived AND status NOT IN ('completed', 'cancelled');

    UPDATE tasks_taskcollaborator AS collaborator
    SET is_active = FALSE
    FROM tasks_task AS task
    WHERE collaborator.organization_id = NEW.organization_id
      AND collaborator.organization_membership_id = target_member_id
      AND collaborator.is_active
      AND task.organization_id = collaborator.organization_id AND task.id = collaborator.task_id
      AND (target_project_id IS NULL OR task.project_id = target_project_id)
      AND NOT task.is_archived AND task.status NOT IN ('completed', 'cancelled');
    RETURN NULL;
END;
$$;

CREATE TRIGGER task_org_membership_end_closes_assignments
    AFTER UPDATE OF is_active ON organizations_organizationmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION tasks_close_membership_assignments('organization');

CREATE TRIGGER task_project_membership_end_closes_assignments
    AFTER UPDATE OF is_active ON projects_projectmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION tasks_close_membership_assignments('project');
"""


REVERSE_SQL = """
DROP TRIGGER task_project_membership_end_closes_assignments ON projects_projectmembership;
DROP TRIGGER task_org_membership_end_closes_assignments ON organizations_organizationmembership;
DROP FUNCTION tasks_close_membership_assignments();
DROP TRIGGER task_collaborator_reference_guard ON tasks_taskcollaborator;
DROP FUNCTION tasks_validate_collaborator();
DROP TRIGGER task_reference_guard ON tasks_task;
DROP FUNCTION tasks_validate_references();
DROP FUNCTION tasks_require_eligible_participant(bigint, bigint, bigint);

ALTER TABLE tasks_taskcollaborator
    DROP CONSTRAINT task_collaborator_member_tenant,
    DROP CONSTRAINT task_collaborator_task_tenant;
ALTER TABLE tasks_task
    DROP CONSTRAINT task_department_tenant,
    DROP CONSTRAINT task_project_tenant,
    DROP CONSTRAINT task_assignee_tenant,
    DROP CONSTRAINT task_author_tenant;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0001_basic_tasks"),
        ("projects", "0004_staffing_integrity"),
    ]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
