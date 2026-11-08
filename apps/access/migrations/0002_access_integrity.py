from django.db import migrations


FORWARD_SQL = """
ALTER TABLE departments_departmentmembership
    ADD CONSTRAINT department_membership_org_member_id
        UNIQUE (organization_id, organization_membership_id, id);

ALTER TABLE access_roleassignment
    ADD CONSTRAINT role_assignment_role_tenant
        FOREIGN KEY (organization_id, role_id)
        REFERENCES access_role (organization_id, id),
    ADD CONSTRAINT role_assignment_member_tenant
        FOREIGN KEY (organization_id, organization_membership_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT role_assignment_department_member_tenant
        FOREIGN KEY (organization_id, organization_membership_id, department_membership_id)
        REFERENCES departments_departmentmembership (organization_id, organization_membership_id, id);

ALTER TABLE access_positionassignment
    ADD CONSTRAINT position_assignment_member_tenant
        FOREIGN KEY (organization_id, organization_membership_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT position_assignment_department_member_tenant
        FOREIGN KEY (organization_id, organization_membership_id, department_membership_id)
        REFERENCES departments_departmentmembership (organization_id, organization_membership_id, id);

CREATE FUNCTION access_prevent_role_reassignment() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'check_violation',
        CONSTRAINT = 'access_role_organization_immutable',
        MESSAGE = 'A role cannot change organizations';
END;
$$;

CREATE TRIGGER access_role_organization_immutable
    BEFORE UPDATE OF organization_id ON access_role
    FOR EACH ROW WHEN (OLD.organization_id IS DISTINCT FROM NEW.organization_id)
    EXECUTE FUNCTION access_prevent_role_reassignment();

CREATE FUNCTION access_validate_assignment() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    role_archived boolean;
    member_active boolean;
    department_member_active boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.organization_id, NEW.organization_membership_id, NEW.department_membership_id)
            IS DISTINCT FROM
            (OLD.organization_id, OLD.organization_membership_id, OLD.department_membership_id)
        THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'access_assignment_identity_immutable',
                MESSAGE = 'An assignment cannot change its organization, participant or scope';
        END IF;

        IF TG_ARGV[0] = 'role' THEN
            IF NEW.role_id IS DISTINCT FROM OLD.role_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    CONSTRAINT = 'access_assignment_identity_immutable',
                    MESSAGE = 'An assignment cannot change its role';
            END IF;
        END IF;

        IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'access_assignment_revocation_terminal',
                MESSAGE = 'A revoked assignment cannot be reactivated';
        END IF;
    END IF;

    IF NEW.revoked_at IS NOT NULL THEN
        RETURN NEW;
    END IF;

    -- Write parent row versions so closure waits for activation, and an older
    -- snapshot at REPEATABLE READ cannot miss a concurrent assignment.
    IF TG_ARGV[0] = 'role' THEN
        UPDATE access_role
        SET is_archived = is_archived
        WHERE organization_id = NEW.organization_id AND id = NEW.role_id
        RETURNING is_archived INTO role_archived;
    END IF;

    UPDATE organizations_organizationmembership
    SET is_active = is_active
    WHERE organization_id = NEW.organization_id AND id = NEW.organization_membership_id
    RETURNING is_active INTO member_active;

    IF NEW.department_membership_id IS NOT NULL THEN
        UPDATE departments_departmentmembership
        SET is_active = is_active
        WHERE organization_id = NEW.organization_id
          AND organization_membership_id = NEW.organization_membership_id
          AND id = NEW.department_membership_id
        RETURNING is_active INTO department_member_active;
    END IF;

    -- Missing or mismatched references are handled by the composite FKs.
    IF role_archived IS TRUE OR member_active IS FALSE OR department_member_active IS FALSE THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'access_assignment_requires_active_parents',
            MESSAGE = 'An active assignment requires active memberships and an unarchived role';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER access_role_assignment_active_parents
    BEFORE INSERT OR UPDATE OF organization_id, role_id, organization_membership_id,
        department_membership_id, revoked_at ON access_roleassignment
    FOR EACH ROW EXECUTE FUNCTION access_validate_assignment('role');

CREATE TRIGGER access_position_assignment_active_parents
    BEFORE INSERT OR UPDATE OF organization_id, organization_membership_id,
        department_membership_id, revoked_at ON access_positionassignment
    FOR EACH ROW EXECUTE FUNCTION access_validate_assignment('position');

CREATE FUNCTION access_revoke_assignments() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_ARGV[0] = 'role' THEN
        UPDATE access_roleassignment
        SET revoked_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id AND role_id = NEW.id AND revoked_at IS NULL;
    ELSIF TG_ARGV[0] = 'organization' THEN
        UPDATE access_roleassignment
        SET revoked_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id
          AND organization_membership_id = NEW.id AND revoked_at IS NULL;

        UPDATE access_positionassignment
        SET revoked_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id
          AND organization_membership_id = NEW.id AND revoked_at IS NULL;
    ELSE
        UPDATE access_roleassignment
        SET revoked_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id
          AND department_membership_id = NEW.id AND revoked_at IS NULL;

        UPDATE access_positionassignment
        SET revoked_at = statement_timestamp()
        WHERE organization_id = NEW.organization_id
          AND department_membership_id = NEW.id AND revoked_at IS NULL;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER access_role_archive_revokes_assignments
    AFTER UPDATE OF is_archived ON access_role
    FOR EACH ROW WHEN (NOT OLD.is_archived AND NEW.is_archived)
    EXECUTE FUNCTION access_revoke_assignments('role');

CREATE FUNCTION access_clear_inactive_legacy_management() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT NEW.is_active THEN
        NEW.can_manage_members := FALSE;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER access_clear_inactive_legacy_management
    BEFORE INSERT OR UPDATE OF is_active, can_manage_members ON organizations_organizationmembership
    FOR EACH ROW EXECUTE FUNCTION access_clear_inactive_legacy_management();

CREATE TRIGGER access_organization_membership_end_revokes_assignments
    AFTER UPDATE OF is_active ON organizations_organizationmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION access_revoke_assignments('organization');

CREATE TRIGGER access_department_membership_end_revokes_assignments
    AFTER UPDATE OF is_active ON departments_departmentmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION access_revoke_assignments('department');
"""


REVERSE_SQL = """
DROP TRIGGER access_department_membership_end_revokes_assignments
    ON departments_departmentmembership;
DROP TRIGGER access_organization_membership_end_revokes_assignments
    ON organizations_organizationmembership;
DROP TRIGGER access_clear_inactive_legacy_management ON organizations_organizationmembership;
DROP FUNCTION access_clear_inactive_legacy_management();
DROP TRIGGER access_role_archive_revokes_assignments ON access_role;
DROP FUNCTION access_revoke_assignments();

DROP TRIGGER access_position_assignment_active_parents ON access_positionassignment;
DROP TRIGGER access_role_assignment_active_parents ON access_roleassignment;
DROP FUNCTION access_validate_assignment();

DROP TRIGGER access_role_organization_immutable ON access_role;
DROP FUNCTION access_prevent_role_reassignment();

ALTER TABLE access_positionassignment
    DROP CONSTRAINT position_assignment_department_member_tenant,
    DROP CONSTRAINT position_assignment_member_tenant;
ALTER TABLE access_roleassignment
    DROP CONSTRAINT role_assignment_department_member_tenant,
    DROP CONSTRAINT role_assignment_member_tenant,
    DROP CONSTRAINT role_assignment_role_tenant;
ALTER TABLE departments_departmentmembership
    DROP CONSTRAINT department_membership_org_member_id;
"""


class Migration(migrations.Migration):
    dependencies = [("access", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
