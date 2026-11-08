from django.db import migrations


FORWARD_SQL = """
ALTER TABLE calendar_app_calendarevent
    ADD CONSTRAINT calendar_event_creator_tenant
        FOREIGN KEY (organization_id, created_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT calendar_event_project_tenant
        FOREIGN KEY (organization_id, project_id)
        REFERENCES projects_project (organization_id, id),
    ADD CONSTRAINT calendar_event_department_tenant
        FOREIGN KEY (organization_id, department_id)
        REFERENCES departments_department (organization_id, id);

CREATE FUNCTION calendar_validate_event_scope() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    check_project boolean := NEW.project_id IS NOT NULL;
    check_department boolean := NEW.department_id IS NOT NULL;
    scope_archived boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.organization_id, NEW.created_by_id)
            IS DISTINCT FROM (OLD.organization_id, OLD.created_by_id)
        THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'calendar_event_identity_immutable',
                MESSAGE = 'A calendar event cannot change its organization or creator';
        END IF;

        check_project := check_project AND NEW.project_id IS DISTINCT FROM OLD.project_id;
        check_department := check_department AND NEW.department_id IS DISTINCT FROM OLD.department_id;
    END IF;

    -- Selecting a new scope writes its row version so it cannot race with
    -- archival. Existing links remain available as historical references.
    IF check_project THEN
        UPDATE projects_project SET is_archived = is_archived
        WHERE organization_id = NEW.organization_id AND id = NEW.project_id
        RETURNING is_archived INTO scope_archived;

        IF scope_archived IS TRUE THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'calendar_event_project_not_archived',
                MESSAGE = 'A new calendar event project cannot be archived';
        END IF;
    END IF;

    IF check_department THEN
        UPDATE departments_department SET is_archived = is_archived
        WHERE organization_id = NEW.organization_id AND id = NEW.department_id
        RETURNING is_archived INTO scope_archived;

        IF scope_archived IS TRUE THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'calendar_event_department_not_archived',
                MESSAGE = 'A new calendar event department cannot be archived';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER calendar_event_scope_guard
    BEFORE INSERT OR UPDATE OF organization_id, created_by_id, project_id, department_id
    ON calendar_app_calendarevent
    FOR EACH ROW EXECUTE FUNCTION calendar_validate_event_scope();
"""


REVERSE_SQL = """
DROP TRIGGER calendar_event_scope_guard ON calendar_app_calendarevent;
DROP FUNCTION calendar_validate_event_scope();
ALTER TABLE calendar_app_calendarevent
    DROP CONSTRAINT calendar_event_department_tenant,
    DROP CONSTRAINT calendar_event_project_tenant,
    DROP CONSTRAINT calendar_event_creator_tenant;
"""


class Migration(migrations.Migration):
    dependencies = [("calendar_app", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
