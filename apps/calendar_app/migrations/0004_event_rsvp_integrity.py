from django.db import migrations


FORWARD_SQL = """
ALTER TABLE calendar_app_calendarevent
    ADD CONSTRAINT calendar_event_rsvp_mode_valid
        CHECK (rsvp_mode IN ('informational', 'optional', 'required'));

ALTER TABLE calendar_app_calendaraudience
    ADD CONSTRAINT calendar_audience_event_tenant
        FOREIGN KEY (organization_id, event_id) REFERENCES calendar_app_calendarevent (organization_id, id),
    ADD CONSTRAINT calendar_audience_creator_tenant
        FOREIGN KEY (organization_id, created_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT calendar_audience_member_tenant
        FOREIGN KEY (organization_id, member_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT calendar_audience_department_tenant
        FOREIGN KEY (organization_id, department_id) REFERENCES departments_department (organization_id, id),
    ADD CONSTRAINT calendar_audience_project_tenant
        FOREIGN KEY (organization_id, project_id) REFERENCES projects_project (organization_id, id);

ALTER TABLE calendar_app_calendarinvitation
    ADD CONSTRAINT calendar_invitation_event_tenant
        FOREIGN KEY (organization_id, event_id) REFERENCES calendar_app_calendarevent (organization_id, id),
    ADD CONSTRAINT calendar_invitation_recipient_tenant
        FOREIGN KEY (organization_id, recipient_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT calendar_invitation_inviter_tenant
        FOREIGN KEY (organization_id, invited_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT calendar_invitation_revoker_tenant
        FOREIGN KEY (organization_id, revoked_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

ALTER TABLE calendar_app_calendarinvitationsource
    ADD CONSTRAINT calendar_invitation_source_invitation_scope
        FOREIGN KEY (organization_id, event_id, invitation_id)
        REFERENCES calendar_app_calendarinvitation (organization_id, event_id, id),
    ADD CONSTRAINT calendar_invitation_source_audience_scope
        FOREIGN KEY (organization_id, event_id, audience_id)
        REFERENCES calendar_app_calendaraudience (organization_id, event_id, id);

ALTER TABLE calendar_app_calendarrsvphistory
    ADD CONSTRAINT calendar_rsvp_history_invitation_scope
        FOREIGN KEY (organization_id, event_id, invitation_id)
        REFERENCES calendar_app_calendarinvitation (organization_id, event_id, id),
    ADD CONSTRAINT calendar_rsvp_history_actor_tenant
        FOREIGN KEY (organization_id, actor_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

CREATE FUNCTION calendar_protect_rsvp_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rsvp_revision < OLD.rsvp_revision THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'calendar_event_rsvp_revision_monotonic',
            MESSAGE = 'A calendar event RSVP revision cannot decrease';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER calendar_event_rsvp_revision_monotonic
    BEFORE UPDATE OF rsvp_revision ON calendar_app_calendarevent
    FOR EACH ROW EXECUTE FUNCTION calendar_protect_rsvp_revision();

CREATE FUNCTION calendar_validate_invitation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    event_status text;
    event_mode text;
    event_revision integer;
    recipient_active boolean;
    response_changed boolean := NEW.response IS NOT NULL;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.organization_id, NEW.event_id, NEW.recipient_id, NEW.invited_by_id)
            IS DISTINCT FROM (OLD.organization_id, OLD.event_id, OLD.recipient_id, OLD.invited_by_id)
        THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'calendar_invitation_identity_immutable',
                MESSAGE = 'A calendar invitation cannot change event or participant';
        END IF;

        response_changed := (NEW.response, NEW.response_revision, NEW.responded_at)
            IS DISTINCT FROM (OLD.response, OLD.response_revision, OLD.responded_at);

        IF OLD.revoked_at IS NOT NULL AND (
            response_changed OR (NEW.revoked_at, NEW.revoked_by_id)
                IS DISTINCT FROM (OLD.revoked_at, OLD.revoked_by_id)
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'calendar_invitation_revocation_terminal',
                MESSAGE = 'A revoked invitation cannot be edited or reactivated';
        END IF;
    END IF;

    IF response_changed AND (NEW.revoked_at IS NOT NULL OR NEW.response IS NULL) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'calendar_invitation_response_preserved',
            MESSAGE = 'Only an active invitation can record a response';
    END IF;

    IF (TG_OP = 'UPDATE' AND NOT response_changed) OR NEW.revoked_at IS NOT NULL THEN
        RETURN NEW;
    END IF;

    -- Service paths already lock organization and event first. NOWAIT also
    -- makes competing raw SQL fail promptly instead of reversing lock order
    -- with membership deactivation or event management.
    SELECT status, rsvp_mode, rsvp_revision INTO event_status, event_mode, event_revision
    FROM calendar_app_calendarevent
    WHERE organization_id = NEW.organization_id AND id = NEW.event_id
    FOR NO KEY UPDATE NOWAIT;

    PERFORM FROM organizations_organizationmembership
    WHERE organization_id = NEW.organization_id AND id = NEW.recipient_id
    FOR NO KEY UPDATE NOWAIT;
    UPDATE organizations_organizationmembership SET is_active = is_active
    WHERE organization_id = NEW.organization_id AND id = NEW.recipient_id
    RETURNING is_active INTO recipient_active;

    -- Missing or foreign records are reported by the composite FKs.
    IF event_status <> 'scheduled' OR recipient_active IS FALSE THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'calendar_invitation_active_parents',
            MESSAGE = 'An invitation requires a scheduled event and an active recipient';
    END IF;

    IF response_changed AND (event_mode = 'informational'
        OR NEW.response_revision IS DISTINCT FROM event_revision)
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'calendar_invitation_response_revision_current',
            MESSAGE = 'An RSVP response requires the current event revision and an active RSVP mode';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER calendar_invitation_response_guard
    BEFORE INSERT OR UPDATE OF organization_id, event_id, recipient_id, invited_by_id,
        revoked_at, revoked_by_id, response, response_revision, responded_at ON calendar_app_calendarinvitation
    FOR EACH ROW EXECUTE FUNCTION calendar_validate_invitation();

CREATE FUNCTION calendar_revoke_departed_member_invitations() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE calendar_app_calendarinvitation
    SET revoked_at = statement_timestamp(), updated_at = statement_timestamp()
    WHERE organization_id = NEW.organization_id AND recipient_id = NEW.id AND revoked_at IS NULL;
    RETURN NULL;
END;
$$;

CREATE TRIGGER calendar_membership_end_revokes_invitations
    AFTER UPDATE OF is_active ON organizations_organizationmembership
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION calendar_revoke_departed_member_invitations();

CREATE FUNCTION calendar_validate_rsvp_history() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    invite_recipient bigint;
    invite_response text;
    invite_revision integer;
BEGIN
    SELECT recipient_id, response, response_revision
    INTO invite_recipient, invite_response, invite_revision
    FROM calendar_app_calendarinvitation
    WHERE organization_id = NEW.organization_id AND event_id = NEW.event_id AND id = NEW.invitation_id;

    IF FOUND AND (NEW.actor_id, NEW.response, NEW.response_revision)
        IS DISTINCT FROM (invite_recipient, invite_response, invite_revision)
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'calendar_rsvp_history_matches_response',
            MESSAGE = 'RSVP history must record the recipient and the current response';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER calendar_rsvp_history_matches_response
    BEFORE INSERT ON calendar_app_calendarrsvphistory
    FOR EACH ROW EXECUTE FUNCTION calendar_validate_rsvp_history();

CREATE FUNCTION calendar_protect_rsvp_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'check_violation',
        CONSTRAINT = 'calendar_rsvp_history_append_only',
        MESSAGE = 'RSVP history cannot be changed or deleted';
END;
$$;

CREATE TRIGGER calendar_rsvp_history_append_only
    BEFORE UPDATE OR DELETE ON calendar_app_calendarrsvphistory
    FOR EACH ROW EXECUTE FUNCTION calendar_protect_rsvp_history();
"""


REVERSE_SQL = """
DROP TRIGGER calendar_rsvp_history_append_only ON calendar_app_calendarrsvphistory;
DROP FUNCTION calendar_protect_rsvp_history();
DROP TRIGGER calendar_rsvp_history_matches_response ON calendar_app_calendarrsvphistory;
DROP FUNCTION calendar_validate_rsvp_history();
DROP TRIGGER calendar_membership_end_revokes_invitations ON organizations_organizationmembership;
DROP FUNCTION calendar_revoke_departed_member_invitations();
DROP TRIGGER calendar_invitation_response_guard ON calendar_app_calendarinvitation;
DROP FUNCTION calendar_validate_invitation();
DROP TRIGGER calendar_event_rsvp_revision_monotonic ON calendar_app_calendarevent;
DROP FUNCTION calendar_protect_rsvp_revision();

ALTER TABLE calendar_app_calendarrsvphistory
    DROP CONSTRAINT calendar_rsvp_history_actor_tenant,
    DROP CONSTRAINT calendar_rsvp_history_invitation_scope;
ALTER TABLE calendar_app_calendarinvitationsource
    DROP CONSTRAINT calendar_invitation_source_audience_scope,
    DROP CONSTRAINT calendar_invitation_source_invitation_scope;
ALTER TABLE calendar_app_calendarinvitation
    DROP CONSTRAINT calendar_invitation_revoker_tenant,
    DROP CONSTRAINT calendar_invitation_inviter_tenant,
    DROP CONSTRAINT calendar_invitation_recipient_tenant,
    DROP CONSTRAINT calendar_invitation_event_tenant;
ALTER TABLE calendar_app_calendaraudience
    DROP CONSTRAINT calendar_audience_project_tenant,
    DROP CONSTRAINT calendar_audience_department_tenant,
    DROP CONSTRAINT calendar_audience_member_tenant,
    DROP CONSTRAINT calendar_audience_creator_tenant,
    DROP CONSTRAINT calendar_audience_event_tenant;
ALTER TABLE calendar_app_calendarevent
    DROP CONSTRAINT calendar_event_rsvp_mode_valid;
"""


class Migration(migrations.Migration):
    dependencies = [("calendar_app", "0003_event_rsvp")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
