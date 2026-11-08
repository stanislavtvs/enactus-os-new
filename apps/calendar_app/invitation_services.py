from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.access.services import organization_access
from apps.departments.services import active_department_members, department_actions
from apps.notifications.producers import emit
from apps.organizations.workspaces import can_view_organization_members
from apps.projects.models import ProjectMembership
from apps.projects.services import _context as project_context
from apps.projects.services import visible_projects

from .audit import calendar_action
from .invitation_models import CalendarAudience, CalendarInvitation, CalendarInvitationSource, CalendarRSVPHistory
from .models import CalendarEvent
from .services import CalendarConflict, _allows, _can_read_event, visible_event

INVITE = "calendar.invite"
MANAGE = "calendar.invite.manage"
MEMBER_PAGE_SIZE = 30


@dataclass(frozen=True)
class Selection:
    kind: str
    member_id: int | None = None
    department_id: int | None = None
    project_id: int | None = None
    include_descendants: bool = False

    def fields(self):
        return {
            "kind": self.kind,
            "member_id": self.member_id,
            "department_id": self.department_id,
            "project_id": self.project_id,
            "include_descendants": self.include_descendants,
        }


def _can_invite(access, event):
    return _can_read_event(access, event) and _allows(access, INVITE, event.project, event.department)


def can_invite(access, event):
    return event.status == CalendarEvent.Status.SCHEDULED and _can_invite(access, event)


def can_manage_invitations(access, event):
    return _can_read_event(access, event) and _allows(access, MANAGE, event.project, event.department)


def _active_recipients(access):
    return access.organization.memberships.filter(is_active=True, user__is_active=True)


def _recipient_invitations(access):
    if access.membership is None:
        return CalendarInvitation.objects.none()
    return CalendarInvitation.objects.filter(
        organization=access.organization, recipient=access.membership, revoked_at__isnull=True,
        recipient__is_active=True, recipient__user__is_active=True,
    )


def has_active_invitation(access, event_id=None):
    rows = _recipient_invitations(access)
    if event_id is not None:
        rows = rows.filter(event_id=event_id)
    return rows.exists()


def annotate_invited(rows, access):
    invitations = _recipient_invitations(access).filter(event_id=OuterRef("pk"))
    return rows.annotate(_invitation_id=Subquery(invitations.order_by("pk").values("pk")[:1]))


def _with_event(rows):
    return rows.select_related("event__organization", "event__created_by__user", "event__recurrence_rule")


def own_invitation(*, access, invitation_id):
    return get_object_or_404(_with_event(_recipient_invitations(access)), pk=invitation_id)


class AudiencePolicy:
    def __init__(self, access):
        self.access = access

    @cached_property
    def can_select_members(self):
        return can_view_organization_members(self.access)

    @property
    def can_select_organization(self):
        return self.can_select_members and self.access.allows(INVITE)

    @cached_property
    def departments(self):
        return {
            department.pk: department
            for department in self.access.organization.departments.filter(is_archived=False).order_by("id")
        }

    @cached_property
    def projects(self):
        return {project.pk: project for project in visible_projects(access=self.access)}

    @cached_property
    def _directory(self):
        return department_actions(user=self.access.user, org_id=self.access.organization.pk)["nodes"]

    @cached_property
    def _children(self):
        children = defaultdict(list)
        for department in self.departments.values():
            children[department.parent_id].append(department.pk)
        return children

    def branch(self, department_id):
        pending = [department_id]
        branch = []
        while pending:
            current = pending.pop()
            branch.append(current)
            pending.extend(self._children[current])
        return branch

    def can_select_department(self, department_id, *, descendants=False):
        department = self.departments.get(department_id)
        if department is None or not self.access.allows(INVITE, department, include_descendants=descendants):
            return False
        ids = self.branch(department_id) if descendants else [department_id]
        return all(
            self._directory.get(node_id, {}).get("open_members", False)
            and self.access.allows(INVITE, self.departments[node_id])
            for node_id in ids
        )

    def can_select_project(self, project):
        return (
            not project.is_archived
            and _allows(self.access, INVITE, project, None)
            and (
                self.access.allows("projects.team.view", project_context(project))
                or getattr(project, "_active_team_member", False)
            )
        )

    def can_manage_source(self, audience):
        if audience.kind == CalendarAudience.Kind.MEMBER:
            return self.can_select_members
        if audience.kind == CalendarAudience.Kind.ORGANIZATION:
            return self.can_select_organization
        if audience.kind == CalendarAudience.Kind.DEPARTMENT:
            if audience.department.is_archived:
                return self.can_select_organization
            return self.can_select_department(audience.department_id, descendants=audience.include_descendants)
        if audience.kind == CalendarAudience.Kind.PROJECT:
            project = self.projects.get(audience.project_id)
            if project is None:
                return False
            if project.is_archived:
                return self.access.allows(INVITE) and (
                    self.access.allows("projects.team.view", project_context(project))
                    or getattr(project, "_active_team_member", False)
                )
            return self.can_select_project(project)
        return False


def _organizer(user, org_id, event_id, *, manage=False, lock=False, scheduled=False):
    access = organization_access(user=user, org_id=org_id, lock=lock)
    event = visible_event(access=access, event_id=event_id, lock=lock)
    permitted = can_manage_invitations(access, event) if manage else _can_invite(access, event)
    if not permitted:
        raise PermissionDenied
    if scheduled and event.status != CalendarEvent.Status.SCHEDULED:
        raise CalendarConflict("Приглашать можно только на запланированное событие.")
    return access, event


def audience_options(*, user, org_id, event_id, q="", page=1):
    access, event = _organizer(user, org_id, event_id, scheduled=True)
    policy = AudiencePolicy(access)
    members = _active_recipients(access).none()
    if policy.can_select_members:
        members = _active_recipients(access).filter(user__username__icontains=q).order_by("user__username", "pk")
    offset = (page - 1) * MEMBER_PAGE_SIZE
    return {
        "members": [
            {"id": member_id, "username": username}
            for member_id, username in members.values_list("id", "user__username")[offset:offset + MEMBER_PAGE_SIZE]
        ],
        "member_total": members.count(),
        "member_page": page,
        "member_page_size": MEMBER_PAGE_SIZE,
        "departments": [
            {"id": department.pk, "name": department.name,
             "can_include_descendants": policy.can_select_department(department.pk, descendants=True)}
            for department in policy.departments.values() if policy.can_select_department(department.pk)
        ],
        "projects": [
            {"id": project.pk, "name": project.name}
            for project in policy.projects.values() if policy.can_select_project(project)
        ],
        "can_invite_organization": policy.can_select_organization,
        "can_reinvite": can_manage_invitations(access, event),
    }


def _selections(*, member_ids, organization, departments, project_ids):
    sources = []
    if organization:
        sources.append(Selection(CalendarAudience.Kind.ORGANIZATION))
    sources.extend(Selection(CalendarAudience.Kind.MEMBER, member_id=member_id) for member_id in member_ids)
    sources.extend(
        Selection(CalendarAudience.Kind.DEPARTMENT, department_id=item["id"],
                  include_descendants=item["include_descendants"])
        for item in departments
    )
    sources.extend(Selection(CalendarAudience.Kind.PROJECT, project_id=project_id) for project_id in project_ids)
    return list(dict.fromkeys(sources))


def _expand(policy, selections):
    if not selections:
        raise CalendarConflict("Выберите хотя бы один источник приглашений.")
    access = policy.access
    recipients = _active_recipients(access)
    member_ids = {selection.member_id for selection in selections if selection.member_id is not None}
    if member_ids and not policy.can_select_members:
        raise PermissionDenied
    if member_ids and recipients.filter(pk__in=member_ids).count() != len(member_ids):
        raise Http404

    department_sources = {}
    project_ids = set()
    for selection in selections:
        if selection.kind == CalendarAudience.Kind.ORGANIZATION and not policy.can_select_organization:
            raise PermissionDenied
        if selection.kind == CalendarAudience.Kind.DEPARTMENT:
            if selection.department_id not in policy.departments:
                raise Http404
            if not policy.can_select_department(selection.department_id, descendants=selection.include_descendants):
                raise PermissionDenied
            department_sources[selection] = (
                policy.branch(selection.department_id) if selection.include_descendants else [selection.department_id]
            )
        if selection.kind == CalendarAudience.Kind.PROJECT:
            project = policy.projects.get(selection.project_id)
            if project is None:
                raise Http404
            if not policy.can_select_project(project):
                raise PermissionDenied
            project_ids.add(project.pk)

    department_members = defaultdict(set)
    department_ids = {department_id for ids in department_sources.values() for department_id in ids}
    for department_id, member_id in active_department_members(access.organization).filter(
        department_id__in=department_ids,
    ).values_list("department_id", "organization_membership_id"):
        department_members[department_id].add(member_id)
    project_members = defaultdict(set)
    for project_id, member_id in ProjectMembership.objects.filter(
        organization=access.organization, project_id__in=project_ids, is_active=True,
        organization_membership__is_active=True, organization_membership__user__is_active=True,
    ).values_list("project_id", "organization_membership_id"):
        project_members[project_id].add(member_id)

    expanded = {}
    for selection in selections:
        if selection.kind == CalendarAudience.Kind.ORGANIZATION:
            expanded[selection] = set(recipients.values_list("id", flat=True))
        elif selection.kind == CalendarAudience.Kind.MEMBER:
            expanded[selection] = {selection.member_id}
        elif selection.kind == CalendarAudience.Kind.DEPARTMENT:
            expanded[selection] = set().union(*(department_members[pk] for pk in department_sources[selection]))
        else:
            expanded[selection] = project_members[selection.project_id]
    return expanded


def _invitation_snapshot(event, expanded):
    selected = set().union(*expanded.values())
    rows = event.invitations.filter(recipient_id__in=selected)
    active = {invitation.recipient_id: invitation for invitation in rows.filter(revoked_at__isnull=True)}
    revoked = set(rows.filter(revoked_at__isnull=False).values_list("recipient_id", flat=True)) - active.keys()
    return selected, active, revoked


def preview_audience(*, user, org_id, event_id, reinvite_revoked=False, **selection):
    access, event = _organizer(user, org_id, event_id, scheduled=True)
    if reinvite_revoked and not can_manage_invitations(access, event):
        raise PermissionDenied
    expanded = _expand(AudiencePolicy(access), _selections(**selection))
    selected, active, revoked = _invitation_snapshot(event, expanded)
    eligible = selected - active.keys() - (set() if reinvite_revoked else revoked)
    return {"total": len(selected), "new": len(eligible), "already_invited": len(active), "revoked": len(revoked)}


def _send_snapshot(access, event, expanded, *, reinvite_revoked=False):
    selected, active, revoked = _invitation_snapshot(event, expanded)
    eligible = selected - active.keys() - (set() if reinvite_revoked else revoked)
    invitations = CalendarInvitation.objects.bulk_create([
        CalendarInvitation(
            organization=access.organization, event=event, recipient_id=member_id,
            invited_by=access.membership,
        )
        for member_id in sorted(eligible)
    ])
    by_recipient = {**active, **{invitation.recipient_id: invitation for invitation in invitations}}
    snapshots = []
    for source, member_ids in expanded.items():
        audience, _ = event.audiences.get_or_create(
            organization=access.organization, **source.fields(), defaults={"created_by": access.membership},
        )
        snapshots.extend(
            CalendarInvitationSource(
                organization=access.organization, event=event, invitation=by_recipient[member_id], audience=audience,
            )
            for member_id in member_ids if member_id in by_recipient
        )
    CalendarInvitationSource.objects.bulk_create(snapshots, ignore_conflicts=True)
    if invitations:
        recipients = access.organization.memberships.filter(pk__in=eligible).values_list("user_id", flat=True)
        emit(access.user, "calendar.invited", "calendar_event", event, recipients,
             key=f"calendar.invite:{invitations[0].pk}")
    return {"total": len(selected), "created": len(invitations), "already_invited": len(active), "revoked": len(revoked)}


@transaction.atomic
def send_invitations(*, user, org_id, event_id, reinvite_revoked=False, **selection):
    access, event = _organizer(user, org_id, event_id, lock=True, scheduled=True)
    if reinvite_revoked and not can_manage_invitations(access, event):
        raise PermissionDenied
    expanded = _expand(AudiencePolicy(access), _selections(**selection))
    return _send_audited_snapshot(access, event, expanded, reinvite_revoked=reinvite_revoked)


@transaction.atomic
def refresh_audience(*, user, org_id, event_id):
    access, event = _organizer(user, org_id, event_id, manage=True, lock=True, scheduled=True)
    if not _can_invite(access, event):
        raise PermissionDenied
    selections = [
        Selection(
            audience.kind, department_id=audience.department_id, project_id=audience.project_id,
            include_descendants=audience.include_descendants,
        )
        for audience in event.audiences.exclude(kind=CalendarAudience.Kind.MEMBER).select_related("department", "project")
        if not (audience.department_id and audience.department.is_archived)
        and not (audience.project_id and audience.project.is_archived)
    ]
    expanded = _expand(AudiencePolicy(access), selections) if selections else {}
    return _send_audited_snapshot(access, event, expanded)


def _send_audited_snapshot(access, event, expanded, *, reinvite_revoked=False):
    before = event.audiences.count()
    sources_before = CalendarInvitationSource.objects.filter(event=event).count()
    result = _send_snapshot(access, event, expanded, reinvite_revoked=reinvite_revoked)
    after = event.audiences.count()
    if result["created"] or before != after or sources_before != CalendarInvitationSource.objects.filter(
        event=event,
    ).count():
        calendar_action(
            access.user, event, "invitations_sent" if result["created"] else "invitation_audience_changed",
            metadata={"before_count": before, "after_count": after, "invitation_count": result["created"]},
        )
    return result


def _managed_invitation_rows(event):
    sources = CalendarInvitationSource.objects.select_related("audience__department", "audience__project")
    return event.invitations.select_related("recipient__user").prefetch_related(
        Prefetch("sources", queryset=sources, to_attr="audience_sources"),
    ).order_by("pk")


def can_revoke_invitation(policy, invitation):
    return (
        invitation.revoked_at is None
        and all(policy.can_manage_source(source.audience) for source in invitation.audience_sources)
    )


def list_event_invitations(*, user, org_id, event_id):
    access, event = _organizer(user, org_id, event_id, manage=True)
    return event, AudiencePolicy(access), _managed_invitation_rows(event)


@transaction.atomic
def revoke_invitation(*, user, org_id, event_id, invitation_id):
    access, event = _organizer(user, org_id, event_id, manage=True, lock=True)
    invitation = get_object_or_404(_managed_invitation_rows(event).select_for_update(of=("self",)), pk=invitation_id)
    if invitation.revoked_at is not None:
        return
    if not can_revoke_invitation(AudiencePolicy(access), invitation):
        raise PermissionDenied
    invitation.revoked_at = timezone.now()
    invitation.revoked_by = access.membership
    invitation.save(update_fields=["revoked_at", "revoked_by", "updated_at"])
    calendar_action(user, event, "invitation_revoked", metadata={"invitation_id": invitation.pk})


def effective_response(invitation, event):
    if event.rsvp_mode == CalendarEvent.RSVPMode.INFORMATIONAL or invitation.response_revision != event.rsvp_revision:
        return None
    return invitation.response


def needs_reconfirmation(invitation, event):
    return (
        invitation.revoked_at is None and invitation.response_revision is not None
        and invitation.response_revision != event.rsvp_revision
        and event.status == CalendarEvent.Status.SCHEDULED
        and event.rsvp_mode != CalendarEvent.RSVPMode.INFORMATIONAL
    )


def invitation_stats(*, user, org_id, event_id):
    access, event = _organizer(user, org_id, event_id, manage=True)
    counts = {"invited": 0, "yes": 0, "no": 0, "maybe": 0, "pending": 0, "needs_reconfirmation": 0, "revoked": 0}
    for invitation in event.invitations.select_related("recipient__user"):
        if invitation.revoked_at is not None or not invitation.recipient.is_active or not invitation.recipient.user.is_active:
            counts["revoked"] += 1
            continue
        counts["invited"] += 1
        counts[effective_response(invitation, event) or "pending"] += 1
        counts["needs_reconfirmation"] += needs_reconfirmation(invitation, event)
    return {
        **counts, "rsvp_mode": event.rsvp_mode, "rsvp_revision": event.rsvp_revision,
        "can_send": can_invite(access, event), "can_manage": True,
    }


def _own_rows(user):
    if not user.is_authenticated or not user.is_active:
        return CalendarInvitation.objects.none()
    return CalendarInvitation.objects.filter(
        recipient__user=user, recipient__is_active=True, recipient__user__is_active=True,
        revoked_at__isnull=True,
    )


def own_invitations(*, user, org_id=None):
    rows = _own_rows(user)
    if org_id is not None:
        rows = rows.filter(organization_id=org_id)
    return _with_event(rows).order_by("event__starts_at", "pk")


def own_invitation_by_id(*, user, invitation_id):
    return get_object_or_404(_with_event(_own_rows(user)), pk=invitation_id)


def _can_respond(invitation, event):
    return (
        invitation.revoked_at is None
        and event.status == CalendarEvent.Status.SCHEDULED
        and event.rsvp_mode in (CalendarEvent.RSVPMode.OPTIONAL, CalendarEvent.RSVPMode.REQUIRED)
    )


def own_invitation_data(invitation):
    from .recurrence import rule_data

    event = invitation.event
    rule = getattr(event, "recurrence_rule", None)
    return {
        "id": invitation.pk,
        "event_id": event.pk,
        "organization_id": event.organization_id,
        "organization_name": event.organization.name,
        "organizer": event.created_by.user.get_username(),
        "rsvp_mode": event.rsvp_mode,
        "rsvp_revision": event.rsvp_revision,
        "response": effective_response(invitation, event),
        "response_revision": invitation.response_revision,
        "responded_at": invitation.responded_at.isoformat() if invitation.responded_at is not None else None,
        "needs_reconfirmation": needs_reconfirmation(invitation, event),
        "can_respond": _can_respond(invitation, event),
        "is_recurring": rule is not None,
        "event": {
            "id": event.pk,
            "title": event.title,
            "source": event.event_type,
            "starts_at": event.starts_at,
            "ends_at": event.ends_at,
            "timezone": event.timezone,
            "all_day": event.all_day,
            "location": event.location or None,
            "online_url": event.online_url or None,
            "status": event.status,
            "status_label": event.get_status_display(),
            "recurrence": rule_data(rule),
        },
    }


def invitation_history(*, user, org_id, event_id, invitation_id):
    access, event = _organizer(user, org_id, event_id, manage=True)
    invitation = get_object_or_404(event.invitations, organization=access.organization, pk=invitation_id)
    return invitation.response_history.filter(
        organization=access.organization, event=event,
    ).select_related("actor__user").order_by("pk")


@transaction.atomic
def respond_to_invitation(*, user, invitation_id, response, expected_revision, expected_response_at):
    locator = get_object_or_404(
        _own_rows(user).values("organization_id", "event_id"), pk=invitation_id,
    )
    access = organization_access(user=user, org_id=locator["organization_id"], lock=True)
    event = get_object_or_404(
        access.organization.calendar_events.select_for_update(of=("self",)).select_related(
            "organization", "created_by__user",
        ),
        pk=locator["event_id"],
    )
    invitation = get_object_or_404(
        _recipient_invitations(access).select_for_update(of=("self",)),
        event=event, pk=invitation_id,
    )
    if not _can_respond(invitation, event):
        raise CalendarConflict("Событие больше не принимает ответы.")
    if expected_revision != event.rsvp_revision:
        raise CalendarConflict("Событие изменилось. Ознакомьтесь с новыми данными и ответьте ещё раз.")
    if invitation.response == response and invitation.response_revision == event.rsvp_revision:
        return invitation
    if invitation.responded_at != expected_response_at:
        raise CalendarConflict("Ответ уже изменился. Обновите приглашение и повторите действие.")

    invitation.response = response
    invitation.response_revision = event.rsvp_revision
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["response", "response_revision", "responded_at", "updated_at"])
    history = CalendarRSVPHistory.objects.create(
        organization=access.organization, event=event, invitation=invitation,
        actor=access.membership, response=response, response_revision=event.rsvp_revision,
    )
    calendar_action(user, event, "rsvp_recorded", metadata={"history_id": history.pk})
    return invitation
