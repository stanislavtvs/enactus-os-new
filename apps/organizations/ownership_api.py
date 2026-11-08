from datetime import datetime
from typing import Annotated

from django.http import HttpResponse
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.security import django_auth
from pydantic import Field, StrictBool, field_validator

from . import ownership

router = Router(auth=django_auth)
RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]


class OwnerMember(Schema):
    id: int
    username: str


class Transfer(Schema):
    id: int
    organization_id: int
    organization_name: str
    from_owner: OwnerMember
    recipient: OwnerMember
    status: str
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None


class TransferOptions(Schema):
    owner: OwnerMember
    pending: Transfer | None
    candidates: list[OwnerMember]


class OfferInput(Schema):
    recipient_id: int = Field(strict=True, gt=0, le=2**63 - 1)
    confirm: StrictBool

    @field_validator("confirm")
    @classmethod
    def require_confirmation(cls, value):
        if not value:
            raise ValueError("Подтвердите передачу владения.")
        return value


class RespondInput(Schema):
    accept: StrictBool


def _mutate(response, mutation, **kwargs):
    response["Cache-Control"] = "no-store"
    try:
        return ownership.offer_data(mutation(**kwargs))
    except ownership.OwnershipConflict as exc:
        raise HttpError(409, str(exc)) from exc


@router.get("/{org_id}/ownership-transfer/", response=TransferOptions, url_name="ownership_transfer")
def options(request, response: HttpResponse, org_id: RecordId):
    response["Cache-Control"] = "no-store"
    return ownership.ownership_options(user=request.user, org_id=org_id)


@router.post("/{org_id}/ownership-transfer/", response={201: Transfer}, url_name="ownership_transfer_offer")
def offer(request, response: HttpResponse, org_id: RecordId, payload: OfferInput):
    return 201, _mutate(
        response, ownership.offer_ownership, user=request.user, org_id=org_id, recipient_id=payload.recipient_id,
    )


@router.post("/{org_id}/ownership-transfer/{transfer_id}/revoke/", response=Transfer)
def revoke(request, response: HttpResponse, org_id: RecordId, transfer_id: RecordId):
    return _mutate(response, ownership.revoke_offer, user=request.user, org_id=org_id, transfer_id=transfer_id)


@router.get("/ownership-transfers/", response=list[Transfer], url_name="ownership_transfer_inbox")
def inbox(request, response: HttpResponse):
    response["Cache-Control"] = "no-store"
    return ownership.own_offers(user=request.user)


@router.post("/ownership-transfers/{transfer_id}/respond/", response=Transfer)
def respond(request, response: HttpResponse, transfer_id: RecordId, payload: RespondInput):
    return _mutate(
        response, ownership.respond_to_offer, user=request.user, transfer_id=transfer_id, accept=payload.accept,
    )
