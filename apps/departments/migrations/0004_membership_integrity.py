from django.db import migrations


FORWARD_SQL = """
ALTER TABLE departments_departmentmembership
    ADD CONSTRAINT department_membership_department_tenant
        FOREIGN KEY (organization_id, department_id)
        REFERENCES departments_department (organization_id, id),
    ADD CONSTRAINT department_membership_member_tenant
        FOREIGN KEY (organization_id, organization_membership_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

ALTER TABLE departments_departmentinvitation
    ADD CONSTRAINT department_invitation_department_tenant
        FOREIGN KEY (organization_id, department_id)
        REFERENCES departments_department (organization_id, id),
    ADD CONSTRAINT department_invitation_member_tenant
        FOREIGN KEY (organization_id, organization_membership_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

CREATE FUNCTION departments_prevent_membership_reassignment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'check_violation',
        CONSTRAINT = 'organization_membership_identity_immutable',
        MESSAGE = 'An organization membership cannot change its organization or user';
END;
$$;

CREATE TRIGGER organization_membership_identity_immutable
    BEFORE UPDATE OF organization_id, user_id ON organizations_organizationmembership
    FOR EACH ROW WHEN (
        (OLD.organization_id, OLD.user_id) IS DISTINCT FROM (NEW.organization_id, NEW.user_id)
    )
    EXECUTE FUNCTION departments_prevent_membership_reassignment();

CREATE FUNCTION departments_validate_engagement() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    archived boolean;
    active boolean;
BEGIN
    IF TG_OP = 'UPDATE' AND
        (NEW.organization_id, NEW.department_id, NEW.organization_membership_id)
        IS DISTINCT FROM
        (OLD.organization_id, OLD.department_id, OLD.organization_membership_id)
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'department_engagement_identity_immutable',
            MESSAGE = 'A department engagement cannot change its related records';
    END IF;

    IF TG_ARGV[0] = 'membership' THEN
        IF NOT NEW.is_active THEN
            RETURN NEW;
        END IF;
    ELSE
        IF NEW.status <> 'pending' THEN
            RETURN NEW;
        END IF;
    END IF;

    -- Row version changes serialize concurrent closure with activation.
    -- At REPEATABLE READ, a closure with a stale snapshot must abort.
    UPDATE departments_department
    SET is_archived = is_archived
    WHERE organization_id = NEW.organization_id AND id = NEW.department_id
    RETURNING is_archived INTO archived;

    UPDATE organizations_organizationmembership
    SET is_active = is_active
    WHERE organization_id = NEW.organization_id AND id = NEW.organization_membership_id
    RETURNING is_active INTO active;

    -- The composite foreign keys separately reject missing or foreign parents.
    IF archived OR NOT active THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'department_engagement_requires_active_parents',
            MESSAGE = 'Department engagement requires an active organization membership and department';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER department_membership_active_parents
    BEFORE INSERT OR UPDATE OF organization_id, department_id, organization_membership_id, is_active
    ON departments_departmentmembership
    FOR EACH ROW EXECUTE FUNCTION departments_validate_engagement('membership');

CREATE TRIGGER department_invitation_active_parents
    BEFORE INSERT OR UPDATE OF organization_id, department_id, organization_membership_id, status
    ON departments_departmentinvitation
    FOR EACH ROW EXECUTE FUNCTION departments_validate_engagement('invitation');

CREATE FUNCTION departments_close_engagements() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_ARGV[0] = 'department' THEN
        UPDATE departments_departmentmembership
        SET is_active = FALSE, ended_at = statement_timestamp()
        WHERE department_id = NEW.id AND organization_id = NEW.organization_id AND is_active;

        UPDATE departments_departmentinvitation
        SET status = 'revoked', resolved_at = statement_timestamp()
        WHERE department_id = NEW.id AND organization_id = NEW.organization_id AND status = 'pending';
    ELSE
        UPDATE departments_departmentmembership
        SET is_active = FALSE, ended_at = statement_timestamp()
        WHERE organization_membership_id = NEW.id
          AND organization_id = NEW.organization_id AND is_active;

        UPDATE departments_departmentinvitation
        SET status = 'revoked', resolved_at = statement_timestamp()
        WHERE organization_membership_id = NEW.id
          AND organization_id = NEW.organization_id AND status = 'pending';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER department_archive_closes_engagements
    AFTER UPDATE OF is_archived ON departments_department
    FOR EACH ROW WHEN (NOT OLD.is_archived AND NEW.is_archived)
    EXECUTE FUNCTION departments_close_engagements('department');

CREATE TRIGGER organization_membership_deactivation_closes_departments
    AFTER UPDATE OF is_active ON organizations_organizationmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION departments_close_engagements('membership');
"""


REVERSE_SQL = """
DROP TRIGGER organization_membership_deactivation_closes_departments
    ON organizations_organizationmembership;
DROP TRIGGER department_archive_closes_engagements ON departments_department;
DROP FUNCTION departments_close_engagements();
DROP TRIGGER department_invitation_active_parents ON departments_departmentinvitation;
DROP TRIGGER department_membership_active_parents ON departments_departmentmembership;
DROP FUNCTION departments_validate_engagement();
DROP TRIGGER organization_membership_identity_immutable ON organizations_organizationmembership;
DROP FUNCTION departments_prevent_membership_reassignment();

ALTER TABLE departments_departmentinvitation
    DROP CONSTRAINT department_invitation_member_tenant,
    DROP CONSTRAINT department_invitation_department_tenant;
ALTER TABLE departments_departmentmembership
    DROP CONSTRAINT department_membership_member_tenant,
    DROP CONSTRAINT department_membership_department_tenant;
"""


class Migration(migrations.Migration):
    dependencies = [("departments", "0003_department_memberships")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
