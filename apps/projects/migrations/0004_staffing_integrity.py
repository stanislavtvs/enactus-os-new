from django.db import migrations


FORWARD_SQL = """
ALTER TABLE projects_projectmembership
    ADD CONSTRAINT project_membership_project_tenant
        FOREIGN KEY (organization_id, project_id) REFERENCES projects_project (organization_id, id),
    ADD CONSTRAINT project_membership_member_tenant
        FOREIGN KEY (organization_id, organization_membership_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT project_membership_source_tenant
        FOREIGN KEY (organization_id, source_department_id)
        REFERENCES departments_department (organization_id, id);

ALTER TABLE projects_staffingrequest
    ADD CONSTRAINT staffing_request_project_tenant
        FOREIGN KEY (organization_id, project_id) REFERENCES projects_project (organization_id, id),
    ADD CONSTRAINT staffing_request_department_tenant
        FOREIGN KEY (organization_id, department_id) REFERENCES departments_department (organization_id, id);

ALTER TABLE projects_staffingallocation
    ADD CONSTRAINT staffing_allocation_request_scope
        FOREIGN KEY (organization_id, project_id, request_id)
        REFERENCES projects_staffingrequest (organization_id, project_id, id),
    ADD CONSTRAINT staffing_allocation_department_member_scope
        FOREIGN KEY (organization_id, organization_membership_id, department_membership_id)
        REFERENCES departments_departmentmembership (organization_id, organization_membership_id, id);

CREATE FUNCTION projects_require_live_project(tenant_id bigint, target_project_id bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    archived boolean;
BEGIN
    -- Serialize new engagements with archive, including at REPEATABLE READ.
    UPDATE projects_project
    SET is_archived = is_archived
    WHERE organization_id = tenant_id AND id = target_project_id
    RETURNING is_archived INTO archived;

    IF archived IS TRUE THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'staffing_requires_live_project',
            MESSAGE = 'A new project engagement requires an unarchived project';
    END IF;
END;
$$;

CREATE FUNCTION projects_validate_project_membership() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    active boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.organization_id, NEW.project_id, NEW.organization_membership_id)
            IS DISTINCT FROM (OLD.organization_id, OLD.project_id, OLD.organization_membership_id)
        THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'project_membership_identity_immutable',
                MESSAGE = 'A project membership cannot change its project or participant';
        END IF;
        IF OLD.is_active AND NEW.is_active
            AND NEW.source_department_id IS NOT DISTINCT FROM OLD.source_department_id
        THEN
            RETURN NEW;
        END IF;
    END IF;

    IF NEW.is_active THEN
        PERFORM projects_require_live_project(NEW.organization_id, NEW.project_id);
        IF NEW.source_department_id IS NOT NULL THEN
            PERFORM projects_require_active_department(NEW.organization_id, NEW.source_department_id);
        END IF;

        UPDATE organizations_organizationmembership
        SET is_active = is_active
        WHERE organization_id = NEW.organization_id AND id = NEW.organization_membership_id
        RETURNING is_active INTO active;

        IF active IS FALSE THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'project_membership_requires_active_member',
                MESSAGE = 'An active project membership requires an active organization membership';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER project_membership_reference_guard
    BEFORE INSERT OR UPDATE OF organization_id, project_id, organization_membership_id,
        source_department_id, is_active ON projects_projectmembership
    FOR EACH ROW EXECUTE FUNCTION projects_validate_project_membership();

CREATE FUNCTION projects_validate_staffing_request() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    check_department boolean := TRUE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.organization_id, NEW.project_id) IS DISTINCT FROM (OLD.organization_id, OLD.project_id) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'staffing_request_identity_immutable',
                MESSAGE = 'A staffing request cannot change projects';
        END IF;

        IF NEW.department_id IS DISTINCT FROM OLD.department_id THEN
            -- Serialize a draft target edit with any allocation branch check.
            UPDATE organizations_organization
            SET join_policy = join_policy
            WHERE id = NEW.organization_id;

            IF OLD.status <> 'draft' OR EXISTS (
                SELECT FROM projects_staffingallocation WHERE request_id = NEW.id
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    CONSTRAINT = 'staffing_request_target_immutable',
                    MESSAGE = 'Only a draft request without allocations can change its target department';
            END IF;
        ELSE
            check_department := OLD.status = 'draft' AND NEW.status = 'sent';
        END IF;
    END IF;

    IF check_department THEN
        PERFORM projects_require_live_project(NEW.organization_id, NEW.project_id);
        PERFORM projects_require_active_department(NEW.organization_id, NEW.department_id);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER staffing_request_reference_guard
    BEFORE INSERT OR UPDATE OF organization_id, project_id, department_id, status ON projects_staffingrequest
    FOR EACH ROW EXECUTE FUNCTION projects_validate_staffing_request();

CREATE FUNCTION projects_validate_staffing_allocation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    source_department bigint;
    source_active boolean;
    target_department bigint;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.organization_id, NEW.project_id, NEW.request_id,
            NEW.organization_membership_id, NEW.department_membership_id, NEW.mode)
            IS DISTINCT FROM (OLD.organization_id, OLD.project_id, OLD.request_id,
                              OLD.organization_membership_id, OLD.department_membership_id, OLD.mode)
        THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'staffing_allocation_identity_immutable',
                MESSAGE = 'A staffing allocation cannot change its request, participant or mode';
        END IF;

        IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
            RETURN NEW;
        END IF;
    END IF;

    IF NEW.status NOT IN ('pending', 'accepted') THEN
        RETURN NEW;
    END IF;

    -- Department tree changes write this same row. The branch check below
    -- observes a concurrent move or aborts at stricter isolation levels.
    UPDATE organizations_organization
    SET join_policy = join_policy
    WHERE id = NEW.organization_id;

    PERFORM projects_require_live_project(NEW.organization_id, NEW.project_id);

    UPDATE departments_departmentmembership
    SET is_active = is_active
    WHERE organization_id = NEW.organization_id
      AND organization_membership_id = NEW.organization_membership_id
      AND id = NEW.department_membership_id
    RETURNING department_id, is_active INTO source_department, source_active;

    IF source_active IS FALSE THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'staffing_allocation_requires_active_source',
            MESSAGE = 'A staffing allocation requires active source department membership';
    END IF;

    SELECT department_id INTO target_department
    FROM projects_staffingrequest
    WHERE organization_id = NEW.organization_id AND project_id = NEW.project_id AND id = NEW.request_id;

    -- The composite keys report missing or mismatched references.
    IF source_department IS NOT NULL AND target_department IS NOT NULL AND NOT EXISTS (
        WITH RECURSIVE ancestors (id, parent_id) AS (
            SELECT id, parent_id FROM departments_department
            WHERE organization_id = NEW.organization_id AND id = source_department
            UNION
            SELECT parent.id, parent.parent_id
            FROM ancestors AS child
            JOIN departments_department AS parent ON parent.id = child.parent_id
            WHERE parent.organization_id = NEW.organization_id
        )
        SELECT FROM ancestors WHERE id = target_department
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'staffing_allocation_source_branch',
            MESSAGE = 'The source department must belong to the requested department branch';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER staffing_allocation_reference_guard
    BEFORE INSERT OR UPDATE OF organization_id, project_id, request_id, organization_membership_id,
        department_membership_id, mode, status ON projects_staffingallocation
    FOR EACH ROW EXECUTE FUNCTION projects_validate_staffing_allocation();

CREATE FUNCTION projects_close_staffing_engagements() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_ARGV[0] = 'project' THEN
        UPDATE projects_staffingallocation
        SET status = 'revoked', resolved_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id AND project_id = NEW.id AND status = 'pending';
    ELSIF TG_ARGV[0] = 'department_membership' THEN
        UPDATE projects_staffingallocation
        SET status = 'revoked', resolved_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id
          AND department_membership_id = NEW.id AND status = 'pending';
    ELSE
        UPDATE projects_projectmembership
        SET is_active = FALSE, ended_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id
          AND organization_membership_id = NEW.id AND is_active;

        UPDATE projects_staffingallocation
        SET status = 'revoked', resolved_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id
          AND organization_membership_id = NEW.id AND status = 'pending';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER project_archive_closes_pending_staffing
    AFTER UPDATE OF is_archived ON projects_project
    FOR EACH ROW WHEN (NOT OLD.is_archived AND NEW.is_archived)
    EXECUTE FUNCTION projects_close_staffing_engagements('project');

CREATE TRIGGER organization_membership_end_closes_project_engagements
    AFTER UPDATE OF is_active ON organizations_organizationmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION projects_close_staffing_engagements('organization_membership');

CREATE TRIGGER department_membership_end_closes_pending_staffing
    AFTER UPDATE OF is_active ON departments_departmentmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION projects_close_staffing_engagements('department_membership');
"""


REVERSE_SQL = """
DROP TRIGGER department_membership_end_closes_pending_staffing ON departments_departmentmembership;
DROP TRIGGER organization_membership_end_closes_project_engagements ON organizations_organizationmembership;
DROP TRIGGER project_archive_closes_pending_staffing ON projects_project;
DROP FUNCTION projects_close_staffing_engagements();

DROP TRIGGER staffing_allocation_reference_guard ON projects_staffingallocation;
DROP FUNCTION projects_validate_staffing_allocation();
DROP TRIGGER staffing_request_reference_guard ON projects_staffingrequest;
DROP FUNCTION projects_validate_staffing_request();
DROP TRIGGER project_membership_reference_guard ON projects_projectmembership;
DROP FUNCTION projects_validate_project_membership();
DROP FUNCTION projects_require_live_project(bigint, bigint);

ALTER TABLE projects_staffingallocation
    DROP CONSTRAINT staffing_allocation_department_member_scope,
    DROP CONSTRAINT staffing_allocation_request_scope;
ALTER TABLE projects_staffingrequest
    DROP CONSTRAINT staffing_request_department_tenant,
    DROP CONSTRAINT staffing_request_project_tenant;
ALTER TABLE projects_projectmembership
    DROP CONSTRAINT project_membership_source_tenant,
    DROP CONSTRAINT project_membership_member_tenant,
    DROP CONSTRAINT project_membership_project_tenant;
"""


class Migration(migrations.Migration):
    dependencies = [("projects", "0003_project_staffing"), ("access", "0002_access_integrity")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
