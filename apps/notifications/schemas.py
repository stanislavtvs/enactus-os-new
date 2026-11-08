from datetime import datetime
from typing import Annotated

from ninja import Schema
from pydantic import AfterValidator, ConfigDict, Field

from .models import NotificationEvent

RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]


def _nonzero(value):
    if value == 0:
        raise ValueError("Notification ID cannot be zero")
    return value


NoticeId = Annotated[int, Field(ge=-(2**63 - 1), le=2**63 - 1), AfterValidator(_nonzero)]


class NotificationFilter(Schema):
    model_config = ConfigDict(extra="forbid")

    category: NotificationEvent.Category | None = None
    org_id: RecordId | None = None


class NotificationQuery(NotificationFilter):
    unread: bool = False
    page: Annotated[int, Field(ge=1, le=2**31 - 1)] = 1
    page_size: Annotated[int, Field(ge=1, le=100)] = 20


class OrganizationOption(Schema):
    id: int
    name: str


class NotificationOutput(Schema):
    id: int
    kind: str
    category: NotificationEvent.Category
    organization_id: int | None
    organization_name: str | None
    title: str
    description: str
    created_at: datetime
    read_at: datetime | None
    available: bool


class NotificationList(Schema):
    items: list[NotificationOutput]
    total: int
    page: int
    page_size: int
    organizations: list[OrganizationOption]


class NotificationCount(Schema):
    unread: int


class NotificationRead(NotificationCount):
    id: int
    read_at: datetime


class NotificationsRead(NotificationCount):
    updated: int


class NotificationLink(Schema):
    url: str
