from django.db import migrations


FORWARD_SQL = """
ALTER TABLE tasks_task
    ADD CONSTRAINT task_explicit_reviewer_tenant
        FOREIGN KEY (organization_id, reviewer_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

ALTER TABLE tasks_taskdistribution
    ADD CONSTRAINT task_distribution_task_tenant
        FOREIGN KEY (organization_id, task_id)
        REFERENCES tasks_task (organization_id, id),
    ADD CONSTRAINT task_distribution_department_tenant
        FOREIGN KEY (organization_id, department_id)
        REFERENCES departments_department (organization_id, id),
    ADD CONSTRAINT task_distribution_requester_tenant
        FOREIGN KEY (organization_id, requested_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT task_distribution_resolver_tenant
        FOREIGN KEY (organization_id, resolved_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id),
    ADD CONSTRAINT task_distribution_assignee_tenant
        FOREIGN KEY (organization_id, assignee_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

ALTER TABLE tasks_taskreviewsubmission
    ADD CONSTRAINT task_review_submission_task_tenant
        FOREIGN KEY (organization_id, task_id)
        REFERENCES tasks_task (organization_id, id),
    ADD CONSTRAINT task_review_submission_submitter_tenant
        FOREIGN KEY (organization_id, submitted_by_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

ALTER TABLE tasks_taskreviewstage
    ADD CONSTRAINT task_review_stage_submission_scope
        FOREIGN KEY (organization_id, task_id, submission_id)
        REFERENCES tasks_taskreviewsubmission (organization_id, task_id, id),
    ADD CONSTRAINT task_review_stage_department_tenant
        FOREIGN KEY (organization_id, department_id)
        REFERENCES departments_department (organization_id, id),
    ADD CONSTRAINT task_review_stage_kind_scope CHECK (
        (kind = 'department' AND department_id IS NOT NULL AND reviewer_id IS NULL)
        OR (kind = 'project' AND reviewer_id IS NULL)
        OR (kind = 'explicit' AND reviewer_id IS NOT NULL)
    ),
    ADD CONSTRAINT task_review_stage_reviewer_tenant
        FOREIGN KEY (organization_id, reviewer_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

ALTER TABLE tasks_taskreviewdecision
    ADD CONSTRAINT task_review_decision_stage_scope
        FOREIGN KEY (organization_id, task_id, stage_id)
        REFERENCES tasks_taskreviewstage (organization_id, task_id, id),
    ADD CONSTRAINT task_review_decision_outcome CHECK (
        outcome = 'approved' OR (outcome = 'returned' AND comment ~ '[^[:space:]]')
    ),
    ADD CONSTRAINT task_review_decision_actor_tenant
        FOREIGN KEY (organization_id, actor_id)
        REFERENCES organizations_organizationmembership (organization_id, id);

CREATE FUNCTION tasks_prevent_review_history_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_review_history_immutable',
            MESSAGE = 'Saved review history cannot be edited';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_review_submission_immutable
    BEFORE UPDATE ON tasks_taskreviewsubmission
    FOR EACH ROW EXECUTE FUNCTION tasks_prevent_review_history_change();

CREATE TRIGGER task_review_stage_immutable
    BEFORE UPDATE ON tasks_taskreviewstage
    FOR EACH ROW EXECUTE FUNCTION tasks_prevent_review_history_change();

CREATE TRIGGER task_review_decision_immutable
    BEFORE UPDATE ON tasks_taskreviewdecision
    FOR EACH ROW EXECUTE FUNCTION tasks_prevent_review_history_change();

CREATE FUNCTION tasks_validate_distribution_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.organization_id, NEW.task_id, NEW.department_id, NEW.mode, NEW.requested_by_id, NEW.created_at)
        IS DISTINCT FROM
        (OLD.organization_id, OLD.task_id, OLD.department_id, OLD.mode, OLD.requested_by_id, OLD.created_at)
        OR (OLD.status <> 'pending' AND NEW IS DISTINCT FROM OLD)
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_distribution_history_immutable',
            MESSAGE = 'Only an unresolved distribution can record a decision';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_distribution_history_immutable
    BEFORE UPDATE ON tasks_taskdistribution
    FOR EACH ROW EXECUTE FUNCTION tasks_validate_distribution_change();

CREATE FUNCTION tasks_validate_review_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    task_status text;
    task_archived boolean;
    assignee bigint;
BEGIN
    UPDATE tasks_task
    SET assignee_id = assignee_id
    WHERE organization_id = NEW.organization_id AND id = NEW.task_id
    RETURNING status, is_archived, assignee_id INTO task_status, task_archived, assignee;

    IF task_status IS NULL THEN
        RETURN NEW;  -- The composite foreign keys reject an invalid task.
    END IF;

    IF task_archived OR task_status NOT IN ('pending', 'in_progress') THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_review_snapshot_open_task',
            MESSAGE = 'A review snapshot requires an open task before review';
    END IF;

    IF TG_ARGV[0] = 'submission' THEN
        IF NEW.submitted_by_id IS DISTINCT FROM assignee THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_review_submitter_is_assignee',
                MESSAGE = 'Only the current assignee can submit the result';
        END IF;
    ELSE
        IF NEW.submission_id IS DISTINCT FROM (
            SELECT max(id) FROM tasks_taskreviewsubmission
            WHERE organization_id = NEW.organization_id AND task_id = NEW.task_id
        ) OR EXISTS (
            SELECT FROM tasks_taskreviewstage AS stage
            JOIN tasks_taskreviewdecision AS decision ON decision.stage_id = stage.id
            WHERE stage.submission_id = NEW.submission_id
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_review_snapshot_current_submission',
                MESSAGE = 'Stages can only be saved for the new submission';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_review_submission_open_task
    BEFORE INSERT ON tasks_taskreviewsubmission
    FOR EACH ROW EXECUTE FUNCTION tasks_validate_review_snapshot('submission');

CREATE TRIGGER task_review_stage_open_task
    BEFORE INSERT ON tasks_taskreviewstage
    FOR EACH ROW EXECUTE FUNCTION tasks_validate_review_snapshot('stage');

CREATE FUNCTION tasks_validate_review_decision() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    task_status text;
    task_archived boolean;
    assignee bigint;
    submission bigint;
    step_position integer;
    explicit_reviewer bigint;
    submitter bigint;
BEGIN
    -- Review operations share the task row. Writing its version forces
    -- serialization or a stale-snapshot failure when decisions compete.
    UPDATE tasks_task
    SET assignee_id = assignee_id
    WHERE organization_id = NEW.organization_id AND id = NEW.task_id
    RETURNING status, is_archived, assignee_id INTO task_status, task_archived, assignee;

    SELECT stage.submission_id, stage.position, stage.reviewer_id, attempt.submitted_by_id
    INTO submission, step_position, explicit_reviewer, submitter
    FROM tasks_taskreviewstage AS stage
    JOIN tasks_taskreviewsubmission AS attempt ON attempt.id = stage.submission_id
    WHERE stage.organization_id = NEW.organization_id AND stage.task_id = NEW.task_id
      AND stage.id = NEW.stage_id;

    -- The composite foreign keys report invalid scoped references.
    IF task_status IS NULL OR submission IS NULL THEN
        RETURN NEW;
    END IF;

    IF task_archived OR task_status <> 'review' OR submission <> (
        SELECT max(id) FROM tasks_taskreviewsubmission
        WHERE organization_id = NEW.organization_id AND task_id = NEW.task_id
    ) OR EXISTS (
        SELECT FROM tasks_taskreviewstage AS prior
        LEFT JOIN tasks_taskreviewdecision AS decision ON decision.stage_id = prior.id
        WHERE prior.submission_id = submission AND prior.position < step_position
          AND decision.outcome IS DISTINCT FROM 'approved'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_review_decision_current_stage',
            MESSAGE = 'Only the next stage of the current review can be decided';
    END IF;

    IF NEW.actor_id = submitter OR NEW.actor_id = assignee
        OR (explicit_reviewer IS NOT NULL AND NEW.actor_id <> explicit_reviewer)
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_review_actor_independent',
            MESSAGE = 'A review must be decided by its authorized independent reviewer';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_review_decision_current_stage
    BEFORE INSERT ON tasks_taskreviewdecision
    FOR EACH ROW EXECUTE FUNCTION tasks_validate_review_decision();

CREATE FUNCTION tasks_require_review_for_status() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    submission bigint;
    submitter bigint;
    route_policy text;
    route_stages text[];
    first_position integer;
    last_position integer;
BEGIN
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'review' AND NEW.status = 'pending' AND NEW.assignee_id IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_review_restart_requires_decision',
            MESSAGE = 'A review returns to pending only after its assignment is cleared';
    END IF;

    IF NEW.status NOT IN ('review', 'completed')
        AND NOT (OLD.status = 'review' AND NEW.status = 'in_progress')
    THEN
        RETURN NEW;
    END IF;

    SELECT id, policy, submitted_by_id INTO submission, route_policy, submitter
    FROM tasks_taskreviewsubmission
    WHERE organization_id = NEW.organization_id AND task_id = NEW.id
    ORDER BY id DESC LIMIT 1;

    IF NEW.status = 'in_progress' THEN
        IF NOT EXISTS (
            SELECT FROM tasks_taskreviewstage AS stage
            JOIN tasks_taskreviewdecision AS decision ON decision.stage_id = stage.id
            WHERE stage.submission_id = submission AND decision.outcome = 'returned'
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'check_violation',
                CONSTRAINT = 'task_review_restart_requires_decision',
                MESSAGE = 'Returning a task to work requires a recorded review decision';
        END IF;
        RETURN NEW;
    END IF;

    SELECT array_agg(kind::text ORDER BY position), min(position), max(position)
    INTO route_stages, first_position, last_position
    FROM tasks_taskreviewstage WHERE submission_id = submission;

    IF route_stages IS NULL OR submitter IS DISTINCT FROM NEW.assignee_id
        OR first_position <> 1 OR last_position <> cardinality(route_stages) OR (
        route_stages <> ARRAY['explicit'] AND route_stages IS DISTINCT FROM CASE route_policy
            WHEN 'project' THEN ARRAY['project']
            WHEN 'department' THEN ARRAY['department']
            WHEN 'department_project' THEN ARRAY['department', 'project']
        END
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_status_requires_review_route',
            MESSAGE = 'A task in review or completed requires a saved review route';
    END IF;

    IF NEW.status = 'completed' AND (OLD.status <> 'review' OR EXISTS (
        SELECT FROM tasks_taskreviewstage AS stage
        LEFT JOIN tasks_taskreviewdecision AS decision ON decision.stage_id = stage.id
        WHERE stage.submission_id = submission
          AND decision.outcome IS DISTINCT FROM 'approved'
    )) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            CONSTRAINT = 'task_completion_requires_approved_review',
            MESSAGE = 'All current review stages must be approved before completion';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER task_status_requires_review
    BEFORE UPDATE OF status ON tasks_task
    FOR EACH ROW EXECUTE FUNCTION tasks_require_review_for_status();
"""


REVERSE_SQL = """
DROP TRIGGER task_status_requires_review ON tasks_task;
DROP FUNCTION tasks_require_review_for_status();
DROP TRIGGER task_review_decision_current_stage ON tasks_taskreviewdecision;
DROP FUNCTION tasks_validate_review_decision();
DROP TRIGGER task_review_stage_open_task ON tasks_taskreviewstage;
DROP TRIGGER task_review_submission_open_task ON tasks_taskreviewsubmission;
DROP FUNCTION tasks_validate_review_snapshot();
DROP TRIGGER task_distribution_history_immutable ON tasks_taskdistribution;
DROP FUNCTION tasks_validate_distribution_change();
DROP TRIGGER task_review_decision_immutable ON tasks_taskreviewdecision;
DROP TRIGGER task_review_stage_immutable ON tasks_taskreviewstage;
DROP TRIGGER task_review_submission_immutable ON tasks_taskreviewsubmission;
DROP FUNCTION tasks_prevent_review_history_change();

ALTER TABLE tasks_taskreviewdecision
    DROP CONSTRAINT task_review_decision_actor_tenant,
    DROP CONSTRAINT task_review_decision_outcome,
    DROP CONSTRAINT task_review_decision_stage_scope;
ALTER TABLE tasks_taskreviewstage
    DROP CONSTRAINT task_review_stage_reviewer_tenant,
    DROP CONSTRAINT task_review_stage_kind_scope,
    DROP CONSTRAINT task_review_stage_department_tenant,
    DROP CONSTRAINT task_review_stage_submission_scope;
ALTER TABLE tasks_taskreviewsubmission
    DROP CONSTRAINT task_review_submission_submitter_tenant,
    DROP CONSTRAINT task_review_submission_task_tenant;
ALTER TABLE tasks_taskdistribution
    DROP CONSTRAINT task_distribution_assignee_tenant,
    DROP CONSTRAINT task_distribution_resolver_tenant,
    DROP CONSTRAINT task_distribution_requester_tenant,
    DROP CONSTRAINT task_distribution_department_tenant,
    DROP CONSTRAINT task_distribution_task_tenant;
ALTER TABLE tasks_task DROP CONSTRAINT task_explicit_reviewer_tenant;
"""


class Migration(migrations.Migration):
    dependencies = [("tasks", "0004_normalize_legacy_review")]

    operations = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
