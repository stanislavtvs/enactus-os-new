from datetime import date, datetime
from typing import Annotated

from ninja import Schema
from pydantic import AwareDatetime, ConfigDict, Field, StringConstraints

from .invitation_models import CalendarInvitation
from .models import CalendarEvent
from .recurrence_schemas import RecurrenceOutput
from .schemas import InputId


class DepartmentSelection(Schema):
    model_config = ConfigDict(extra="forbid")

    id: InputId
    include_descendants: Annotated[bool, Field(strict=True)] = False


class AudienceInput(Schema):
    model_config = ConfigDict(extra="forbid")

    member_ids: Annotated[list[InputId], Field(max_length=500)] = Field(default_factory=list)
    organization: Annotated[bool, Field(strict=True)] = False
    departments: Annotated[list[DepartmentSelection], Field(max_length=100)] = Field(default_factory=list)
    project_ids: Annotated[list[InputId], Field(max_length=100)] = Field(default_factory=list)
    reinvite_revoked: Annotated[bool, Field(strict=True)] = False


class RecipientOptionsQuery(Schema):
    q: Annotated[str, StringConstraints(strip_whitespace=True, max_length=150, pattern=r"^[^\x00]*$")] = ""
    page: Annotated[int, Field(ge=1, le=2**31 - 1)] = 1


class MemberOption(Schema):
    id: int
    username: str


class GroupOption(Schema):
    id: int
    name: str


class DepartmentOption(GroupOption):
    can_include_descendants: bool


class RecipientOptions(Schema):
    members: list[MemberOption]
    member_total: int
    member_page: int
    member_page_size: int
    departments: list[DepartmentOption]
    projects: list[GroupOption]
    can_invite_organization: bool
    can_reinvite: bool


class AudiencePreview(Schema):
    total: int
    new: int
    already_invited: int
    revoked: int


class AudienceSent(Schema):
    total: int
    created: int
    already_invited: int
    revoked: int


class ManagedInvitation(Schema):
    id: int
    username: str
    recipient_id: int
    response: CalendarInvitation.Response | None
    responded_at: datetime | None
    revoked_at: datetime | None
    needs_reconfirmation: bool
    can_revoke: bool
    created_at: datetime
    sent_at: datetime


class ManagedInvitations(Schema):
    items: list[ManagedInvitation]


class RSVPStats(Schema):
    invited: int
    yes: int
    no: int
    maybe: int
    pending: int
    needs_reconfirmation: int
    revoked: int
    rsvp_mode: CalendarEvent.RSVPMode
    rsvp_revision: int
    can_send: bool
    can_manage: bool


class RSVPAnswerInput(Schema):
    model_config = ConfigDict(extra="forbid")

    response: CalendarInvitation.Response
    expected_revision: Annotated[int, Field(gt=0, le=2**31 - 1, strict=True)]
    expected_response_at: AwareDatetime | None


class InvitationEvent(Schema):
    id: int
    title: str
    source: CalendarEvent.Type
    starts_at: datetime
    ends_at: datetime
    timezone: str
    all_day: bool
    location: str | None
    online_url: str | None
    status: CalendarEvent.Status
    status_label: str
    recurrence: RecurrenceOutput | None = None


class OwnInvitation(Schema):
    id: int
    event_id: int
    organization_id: int
    organization_name: str
    organizer: str
    rsvp_mode: CalendarEvent.RSVPMode
    rsvp_revision: int
    response: CalendarInvitation.Response | None
    response_revision: int | None
    responded_at: str | None
    needs_reconfirmation: bool
    can_respond: bool
    is_recurring: bool = False
    event: InvitationEvent


class OwnOccurrenceInvitation(OwnInvitation):
    occurrence_key: date
    occurrence_rsvp_revision: int
    series_response: CalendarInvitation.Response | None


class OccurrenceRSVPInput(RSVPAnswerInput):
    expected_occurrence_revision: Annotated[int, Field(ge=0, le=2**31 - 1, strict=True)]


class OwnInvitations(Schema):
    items: list[OwnInvitation]


class RSVPHistoryEntry(Schema):
    id: int
    response: CalendarInvitation.Response
    response_revision: int
    actor: str
    created_at: datetime


class RSVPHistoryOutput(Schema):
    items: list[RSVPHistoryEntry]


class OccurrenceRSVPHistoryEntry(RSVPHistoryEntry):
    occurrence_rsvp_revision: int


class OccurrenceRSVPHistoryOutput(Schema):
    items: list[OccurrenceRSVPHistoryEntry]
