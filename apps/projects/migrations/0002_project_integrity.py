from django.db import migrations


FORWARD_SQL = """
ALTER TABLE projects_project
    ADD CONSTRAINT project_responsible_department_tenant
        FOREIGN KEY (organization_id, responsible_department_id)
        REFERENCES departments_department (organization_id, id),
    ADD CONSTRAINT project_lead_membership_tenant
        FOREIGN KEY (organization_id, lead_membership_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

ALTER TABLE projects_projectdepartmentlink
    ADD CONSTRAINT project_department_link_project_tenant
        FOREIGN KEY (organization_id, project_id)
        REFERENCES projects_project (organization_id, id),
    ADD CONSTRAINT project_department_link_department_tenant
        FOREIGN KEY (organization_id, department_id)
        REFERENCES departments_department (organization_id, id);

CREATE FUNCTION projects_require_active_department(tenant_id bigint, department_id bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    archived boolean;
BEGIN
    -- Write a new parent row version so that a stale repeatable-read snapshot
    -- cannot create a reference after a concurrent archival.
    UPDATE departments_department
    SET is_archived = is_archived
    WHERE organization_id = tenant_id AND id = department_id
    RETURNING is_archived INTO archived;

    -- Missing or foreign references are checked by the composite foreign key.
    IF archived IS TRUE THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'project_requires_active_department',
            MESSAGE = 'A new project department must not be archived';
    END IF;
END;
$$;

CREATE FUNCTION projects_validate_references() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    check_department boolean := NEW.responsible_department_id IS NOT NULL;
    check_lead boolean := NEW.lead_membership_id IS NOT NULL;
    lead_active boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.organization_id IS DISTINCT FROM NEW.organization_id THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'project_organization_immutable',
                MESSAGE = 'A project cannot change organizations';
        END IF;

        check_department := check_department
            AND NEW.responsible_department_id IS DISTINCT FROM OLD.responsible_department_id;
        check_lead := check_lead AND (
            NEW.lead_membership_id IS DISTINCT FROM OLD.lead_membership_id
            OR (OLD.is_archived AND NOT NEW.is_archived)
        );
    END IF;

    IF check_department THEN
        PERFORM projects_require_active_department(NEW.organization_id, NEW.responsible_department_id);
    END IF;

    IF check_lead THEN
        UPDATE organizations_organizationmembership
        SET is_active = is_active
        WHERE organization_id = NEW.organization_id AND id = NEW.lead_membership_id
        RETURNING is_active INTO lead_active;

        IF lead_active IS FALSE THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'project_requires_active_lead',
                MESSAGE = 'A project lead must have an active organization membership';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER project_reference_guard
    BEFORE INSERT OR UPDATE OF organization_id, responsible_department_id, lead_membership_id,
        is_archived ON projects_project
    FOR EACH ROW EXECUTE FUNCTION projects_validate_references();

CREATE FUNCTION projects_validate_department_link() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.organization_id, OLD.project_id, OLD.department_id)
            IS DISTINCT FROM (NEW.organization_id, NEW.project_id, NEW.department_id)
        THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'project_department_link_identity_immutable',
                MESSAGE = 'A project department link cannot change its related records';
        END IF;
    ELSE
        PERFORM projects_require_active_department(NEW.organization_id, NEW.department_id);
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER project_department_link_reference_guard
    BEFORE INSERT OR UPDATE OF organization_id, project_id, department_id
    ON projects_projectdepartmentlink
    FOR EACH ROW EXECUTE FUNCTION projects_validate_department_link();

CREATE FUNCTION projects_clear_inactive_lead() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE projects_project
    SET lead_membership_id = NULL, updated_at = statement_timestamp()
    WHERE organization_id = NEW.organization_id
      AND lead_membership_id = NEW.id
      AND NOT is_archived;

    RETURN NULL;
END;
$$;

CREATE TRIGGER project_clear_inactive_lead
    AFTER UPDATE OF is_active ON organizations_organizationmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION projects_clear_inactive_lead();
"""


REVERSE_SQL = """
DROP TRIGGER project_clear_inactive_lead ON organizations_organizationmembership;
DROP FUNCTION projects_clear_inactive_lead();
DROP TRIGGER project_department_link_reference_guard ON projects_projectdepartmentlink;
DROP FUNCTION projects_validate_department_link();
DROP TRIGGER project_reference_guard ON projects_project;
DROP FUNCTION projects_validate_references();
DROP FUNCTION projects_require_active_department(bigint, bigint);

ALTER TABLE projects_projectdepartmentlink
    DROP CONSTRAINT project_department_link_department_tenant,
    DROP CONSTRAINT project_department_link_project_tenant;
ALTER TABLE projects_project
    DROP CONSTRAINT project_lead_membership_tenant,
    DROP CONSTRAINT project_responsible_department_tenant;
"""


class Migration(migrations.Migration):
    dependencies = [("projects", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
