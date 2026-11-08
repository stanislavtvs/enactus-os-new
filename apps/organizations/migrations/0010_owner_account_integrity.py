from django.db import migrations


FORWARD_SQL = """
CREATE FUNCTION organizations_require_active_owner_account() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    owner_active boolean;
BEGIN
    -- Write a tuple version without changing account data. A concurrent
    -- deactivation must either observe the new owner or fail serialization
    -- under a stale repeatable-read snapshot.
    UPDATE accounts_user SET last_login = last_login
    WHERE id = NEW.owner_id
    RETURNING is_active INTO owner_active;

    IF owner_active IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'organization_owner_account_active',
            MESSAGE = 'An organization owner must have an active account';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER organization_owner_account_insert_guard
    BEFORE INSERT ON organizations_organization
    FOR EACH ROW EXECUTE FUNCTION organizations_require_active_owner_account();

CREATE TRIGGER organization_owner_account_change_guard
    BEFORE UPDATE OF owner_id ON organizations_organization
    FOR EACH ROW WHEN (OLD.owner_id IS DISTINCT FROM NEW.owner_id)
    EXECUTE FUNCTION organizations_require_active_owner_account();

CREATE FUNCTION organizations_protect_owner_account() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- PostgreSQL already holds this user row for the outer write. Keep the
    -- organization lookup nonlocking to avoid reversing the transfer locks.
    IF EXISTS (SELECT FROM organizations_organization WHERE owner_id = OLD.id) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'organization_owner_account_protected',
            MESSAGE = 'Transfer organization ownership before disabling or deleting this account';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER organization_owner_account_deactivation_guard
    BEFORE UPDATE OF is_active ON accounts_user
    FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
    EXECUTE FUNCTION organizations_protect_owner_account();

CREATE TRIGGER organization_owner_account_delete_guard
    BEFORE DELETE ON accounts_user
    FOR EACH ROW EXECUTE FUNCTION organizations_protect_owner_account();
"""


REVERSE_SQL = """
DROP TRIGGER organization_owner_account_delete_guard ON accounts_user;
DROP TRIGGER organization_owner_account_deactivation_guard ON accounts_user;
DROP FUNCTION organizations_protect_owner_account();
DROP TRIGGER organization_owner_account_change_guard ON organizations_organization;
DROP TRIGGER organization_owner_account_insert_guard ON organizations_organization;
DROP FUNCTION organizations_require_active_owner_account();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("departments", "0004_membership_integrity"),
        ("organizations", "0009_platform_creation"),
    ]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
