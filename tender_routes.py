"""
tender_routes.py

Handles the Tenders/Bids feature (reverse-bidding marketplace) for ImportEase:
- Importer posts a draft shipment to the Clearing Agent Board as a "tender"
- Clearing agents browse open tenders and submit bids (fee + timeline)
- Importer compares bids and accepts one, which assigns that agent to the shipment

Firestore collections: "tenders", "bids"

Known assumptions / gaps (flagging for review):
- Shipment doesn't currently store a "volume" field, so Tender.volume is left
  as None until Shipments is extended with one.
- Tender.port is derived from the shipment's `destination` field (assumed to
  be the Sri Lankan port/city of entry).
- Tender.requiredPermits is pulled from the HS code's stored `compliance`
  list (plain Firestore read, no embeddings call -- keeps this independent
  from the HS Codes search logic, same principle as Shipments).
- Bid.agentRating is left as None -- Agent Ratings isn't built yet.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from firebase_setup import db
from dependencies import verify_token, require_role
from notification_routes import create_notification

router = APIRouter()

TENDERS_COLLECTION = "tenders"
BIDS_COLLECTION = "bids"
SHIPMENTS_COLLECTION = "shipments"
HSCODES_COLLECTION = "hscodes"
USERS_COLLECTION = "users"
AGENCIES_COLLECTION = "agencies"


# ---------- Request models ----------

class BidCreate(BaseModel):
    feeLkr: float
    clearanceTimelineHours: int
    notes: Optional[str] = None


class TenderCreate(BaseModel):
    # When set, this tender is a direct request aimed at one agency (or one
    # independent agent -- their "agency" IS just themselves) instead of the
    # open board. Only agents at that agency see it in GET /tenders.
    targetAgencyId: Optional[str] = None


# ---------- Helpers ----------

def _get_or_404(collection: str, doc_id: str, not_found_msg: str):
    doc_ref = db.collection(collection).document(doc_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail=not_found_msg)
    return doc_ref, doc


def _get_required_permits(hs_code: str):
    """Pulls the requirement names off the HS code's stored compliance list."""
    if not hs_code:
        return []
    doc = db.collection(HSCODES_COLLECTION).document(hs_code).get()
    if not doc.exists:
        return []
    compliance = doc.to_dict().get("compliance", [])
    return [item.get("requirement") for item in compliance if item.get("requirement")]


# ---------- Tenders ----------

@router.post("/shipments/{shipment_id}/tender")
def create_tender(
    shipment_id: str,
    payload: Optional[TenderCreate] = None,
    user: dict = Depends(require_role("importer")),
):
    """Importer posts their (draft) shipment to the Clearing Agent Board, or
    -- when payload.targetAgencyId is set -- sends it as a direct request to
    one specific agency/independent agent instead."""
    shipment_ref, shipment_doc = _get_or_404(SHIPMENTS_COLLECTION, shipment_id, "Shipment not found")
    shipment_data = shipment_doc.to_dict()

    if shipment_data.get("importerId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the owning importer can post this shipment to tender")

    if shipment_data.get("currentStage") != "draft":
        raise HTTPException(status_code=400, detail="Only draft shipments can be posted to tender")

    existing_open = (
        db.collection(TENDERS_COLLECTION)
        .where("shipmentId", "==", shipment_id)
        .where("status", "==", "open")
        .limit(1)
        .get()
    )
    if len(existing_open) > 0:
        raise HTTPException(status_code=400, detail="This shipment already has an open tender")

    hs_code_check = shipment_data.get("hsCode")
    if hs_code_check:
        duplicate_hs_code = (
            db.collection(TENDERS_COLLECTION)
            .where("importerId", "==", user["uid"])
            .where("hsCode", "==", hs_code_check)
            .where("status", "==", "open")
            .limit(1)
            .get()
        )
        if len(duplicate_hs_code) > 0:
            raise HTTPException(
                status_code=400,
                detail=f"You already have an open request for HS code {hs_code_check}. "
                       "Withdraw it or wait for it to close before posting another.",
            )

    target_agency_id = payload.targetAgencyId if payload else None
    target_agency_doc = None
    if target_agency_id:
        target_agency_doc = db.collection(AGENCIES_COLLECTION).document(target_agency_id).get()
        if not target_agency_doc.exists or target_agency_doc.to_dict().get("profileStatus") != "active":
            raise HTTPException(status_code=400, detail="Selected agent is not available for direct requests")

    hs_code = shipment_data.get("hsCode")
    tender_ref = db.collection(TENDERS_COLLECTION).document()
    tender_data = {
        "shipmentId": shipment_id,
        "importerId": shipment_data.get("importerId"),
        "port": shipment_data.get("destination"),
        "hsCode": hs_code,
        "volume": None,  # not tracked on Shipment yet
        "requiredPermits": _get_required_permits(hs_code),
        # Copied from the shipment so agents browsing the open board can see
        # enough to decide whether to bid without needing shipment access --
        # GET /shipments/{id} is restricted to the owning importer/assigned
        # agent, and nobody is assigned yet at tender time.
        "description": shipment_data.get("description"),
        "declaredValue": shipment_data.get("declaredValue"),
        "origin": shipment_data.get("origin"),
        "estimatedArrival": shipment_data.get("estimatedArrival"),
        "mustReleaseBy": shipment_data.get("mustReleaseBy"),
        # Deliberately NOT copied: contactPhone/contactEmail. Those stay on the
        # Shipment doc, only visible via GET /shipments/{id} once an agent is
        # actually assigned -- no reason to expose an importer's contact
        # details to every agent browsing the open board.
        "targetAgencyId": target_agency_id,
        # Denormalized at post time so the SME's "your requests" list can
        # label a direct request without a per-card agency lookup.
        "targetAgencyName": target_agency_doc.to_dict().get("companyName") if target_agency_doc else None,
        "status": "open",
        "postedAt": datetime.now(timezone.utc).isoformat(),
    }
    tender_ref.set(tender_data)

    if target_agency_id:
        importer_doc = db.collection(USERS_COLLECTION).document(user["uid"]).get()
        importer_name = importer_doc.to_dict().get("name") if importer_doc.exists else None
        # Notifying just the agency admin -- there's no per-agent inbox routing
        # within an agency yet, so the admin is the point of contact for a
        # direct request the same way they're the one who completes the
        # agency's profile and manages its members.
        create_notification(
            user_id=target_agency_doc.to_dict().get("adminUid"),
            shipment_id=shipment_id,
            message=f"New direct clearing request from {importer_name or 'an importer'}.",
            notif_type="direct_request",
            tender_id=tender_ref.id,
        )

    return {"id": tender_ref.id, **tender_data}


@router.get("/tenders")
def list_tenders(status: Optional[str] = None, mine: bool = False, user: dict = Depends(verify_token)):
    """Clearing Agent Board -- lists tenders. Filter with ?status=open or
    ?status=closed. ?mine=true returns the calling importer's own tenders
    (open and closed, including direct requests) regardless of who they were
    targeted at -- this is what powers "your requests" on FindAgent.

    For everyone else (agents browsing), a tender that was sent as a direct
    request to one agency is hidden from every other agency's view."""
    query = db.collection(TENDERS_COLLECTION)
    if status:
        query = query.where("status", "==", status)

    tenders = [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]

    if mine:
        return [t for t in tenders if t.get("importerId") == user["uid"]]

    user_doc = db.collection(USERS_COLLECTION).document(user["uid"]).get()
    profile = user_doc.to_dict() if user_doc.exists else {}
    if profile.get("role") == "clearing_agent":
        agency_id = profile.get("agencyId")
        return [
            t for t in tenders
            if not t.get("targetAgencyId") or t.get("targetAgencyId") == agency_id
        ]

    return tenders


@router.get("/tenders/{tender_id}")
def get_tender(tender_id: str, user: dict = Depends(verify_token)):
    _, doc = _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    return {"id": doc.id, **doc.to_dict()}


@router.delete("/tenders/{tender_id}")
def delete_tender(tender_id: str, user: dict = Depends(require_role("importer"))):
    """SME withdraws one of their own requests. Only allowed while it's still
    open -- once an agent is assigned there's nothing left to withdraw, and
    there's deliberately no "update a posted request" endpoint: withdraw and
    repost instead. Cascades to every bid on it (with a heads-up notification
    to whoever had a pending bid in) and the underlying draft shipment, since
    neither serves any purpose once the request is gone."""
    tender_ref, tender_doc = _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    tender_data = tender_doc.to_dict()

    if tender_data.get("importerId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the owning importer can withdraw this request")

    if tender_data.get("status") != "open":
        raise HTTPException(status_code=400, detail="Only an open request (not yet assigned) can be withdrawn")

    for bid_doc in db.collection(BIDS_COLLECTION).where("tenderId", "==", tender_id).stream():
        bid_data = bid_doc.to_dict()
        if bid_data.get("status") == "pending":
            create_notification(
                user_id=bid_data.get("agentId"),
                shipment_id=tender_data.get("shipmentId"),
                message="An SME withdrew a request you had a bid on.",
                notif_type="tender_withdrawn",
            )
        db.collection(BIDS_COLLECTION).document(bid_doc.id).delete()

    tender_ref.delete()
    db.collection(SHIPMENTS_COLLECTION).document(tender_data["shipmentId"]).delete()

    return {"detail": "Request withdrawn"}


# ---------- Bids ----------

@router.get("/tenders/{tender_id}/bids")
def list_bids(tender_id: str, user: dict = Depends(verify_token)):
    _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    query = db.collection(BIDS_COLLECTION).where("tenderId", "==", tender_id)
    return [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]


@router.get("/bids/mine")
def list_my_bids(user: dict = Depends(verify_token)):
    """Every bid the calling agent has placed, across all tenders -- powers
    their "My Bids" list, which has no other way to enumerate its own bids
    since GET /tenders/{id}/bids needs a tender id up front."""
    query = db.collection(BIDS_COLLECTION).where("agentId", "==", user["uid"])
    return [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]


def _check_agent_can_bid(user: dict):
    """Enforces: role clearing_agent, agentStatus approved, own profile complete,
    agency profileStatus active, and -- for real multi-agent agencies only --
    not an agency admin (admins manage, don't bid). Independent solo agents are
    also flagged isAgencyAdmin, but they ARE the one doing the work, so they're
    exempt from that last rule."""
    user_doc = db.collection(USERS_COLLECTION).document(user["uid"]).get()
    if not user_doc.exists:
        raise HTTPException(status_code=403, detail="User profile not found")
    user_data = user_doc.to_dict()

    if user_data.get("role") != "clearing_agent":
        raise HTTPException(status_code=403, detail="Only clearing agents can submit bids")

    if user_data.get("agentStatus") != "approved":
        raise HTTPException(status_code=403, detail="Agent is not approved")

    if not user_data.get("profileComplete"):
        raise HTTPException(status_code=403, detail="Agent profile is incomplete")

    agency_id = user_data.get("agencyId")
    agency_doc = db.collection(AGENCIES_COLLECTION).document(agency_id).get() if agency_id else None
    if not agency_doc or not agency_doc.exists:
        raise HTTPException(status_code=403, detail="Agency profile is incomplete")

    agency_data = agency_doc.to_dict()
    if agency_data.get("profileStatus") != "active":
        raise HTTPException(status_code=403, detail="Agency profile is incomplete")

    if user_data.get("isAgencyAdmin") and not agency_data.get("isIndependent", False):
        raise HTTPException(status_code=403, detail="Agency admins cannot place bids")

    return user_data, agency_data


@router.post("/tenders/{tender_id}/bids")
def submit_bid(tender_id: str, bid: BidCreate, user: dict = Depends(verify_token)):
    tender_ref, tender_doc = _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    if tender_doc.to_dict().get("status") != "open":
        raise HTTPException(status_code=400, detail="This tender is no longer open")

    agent_data, agency_data = _check_agent_can_bid(user)

    bid_ref = db.collection(BIDS_COLLECTION).document()
    bid_data = {
        "tenderId": tender_id,
        "agentId": user["uid"],
        # Denormalized at submit time so the importer's bid review list can
        # show who's bidding with zero extra lookups (there's no public
        # "get agent info" endpoint, and bids can massively outnumber agents).
        "agentName": agent_data.get("name"),
        "agencyName": agency_data.get("companyName"),
        "feeLkr": bid.feeLkr,
        "clearanceTimelineHours": bid.clearanceTimelineHours,
        "notes": bid.notes,
        "agentRating": None,  # Agent Ratings feature not built yet
        "status": "pending",
        "submittedAt": datetime.now(timezone.utc).isoformat(),
    }
    bid_ref.set(bid_data)

    tender_data = tender_doc.to_dict()
    create_notification(
        user_id=tender_data.get("importerId"),
        shipment_id=tender_data.get("shipmentId"),
        message=(
            f"New {'pitch' if tender_data.get('targetAgencyId') else 'bid'} from "
            f"{agent_data.get('name') or 'a clearing agent'} "
            f"({agency_data.get('companyName') or 'agency'}) -- Rs. {bid.feeLkr:,.0f}"
        ),
        notif_type="new_bid",
        tender_id=tender_id,
    )

    return {"id": bid_ref.id, **bid_data}


def _get_bid_or_404(tender_id: str, bid_id: str):
    bid_ref, bid_doc = _get_or_404(BIDS_COLLECTION, bid_id, "Bid not found")
    if bid_doc.to_dict().get("tenderId") != tender_id:
        raise HTTPException(status_code=404, detail="Bid not found on this tender")
    return bid_ref, bid_doc


@router.get("/tenders/{tender_id}/bids/{bid_id}")
def get_bid(tender_id: str, bid_id: str, user: dict = Depends(verify_token)):
    _, tender_doc = _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    _, bid_doc = _get_bid_or_404(tender_id, bid_id)
    bid_data = bid_doc.to_dict()

    uid = user["uid"]
    if bid_data.get("agentId") != uid and tender_doc.to_dict().get("importerId") != uid:
        raise HTTPException(status_code=403, detail="Not authorized to view this bid")

    return {"id": bid_doc.id, **bid_data}


@router.put("/tenders/{tender_id}/bids/{bid_id}")
def update_bid(tender_id: str, bid_id: str, bid: BidCreate, user: dict = Depends(verify_token)):
    bid_ref, bid_doc = _get_bid_or_404(tender_id, bid_id)
    bid_data = bid_doc.to_dict()

    if bid_data.get("agentId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the bidding agent can update this bid")

    if bid_data.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Only a pending bid can be updated")

    update_fields = {
        "feeLkr": bid.feeLkr,
        "clearanceTimelineHours": bid.clearanceTimelineHours,
        "notes": bid.notes,
    }
    bid_ref.update(update_fields)

    return {"id": bid_id, **bid_ref.get().to_dict()}


@router.delete("/tenders/{tender_id}/bids/{bid_id}")
def withdraw_bid(tender_id: str, bid_id: str, user: dict = Depends(verify_token)):
    bid_ref, bid_doc = _get_bid_or_404(tender_id, bid_id)
    bid_data = bid_doc.to_dict()

    if bid_data.get("agentId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the bidding agent can withdraw this bid")

    if bid_data.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Only a pending bid can be withdrawn")

    bid_ref.delete()
    return {"detail": "Bid withdrawn"}


@router.post("/tenders/{tender_id}/bids/{bid_id}/accept")
def accept_bid(tender_id: str, bid_id: str, user: dict = Depends(verify_token)):
    """Importer accepts a bid: winning bid -> accepted, other pending bids on
    the same tender -> rejected, tender -> closed, shipment -> agentId set +
    stage advances from draft to assigned."""
    tender_ref, tender_doc = _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    tender_data = tender_doc.to_dict()

    if tender_data.get("importerId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the owning importer can accept a bid")

    if tender_data.get("status") != "open":
        raise HTTPException(status_code=400, detail="This tender is already closed")

    bid_ref, bid_doc = _get_bid_or_404(tender_id, bid_id)
    bid_data = bid_doc.to_dict()

    if bid_data.get("status") != "pending":
        raise HTTPException(status_code=400, detail="This bid is no longer pending")

    # Accept the winning bid, reject every other pending bid on this tender
    bid_ref.update({"status": "accepted"})
    other_bids = db.collection(BIDS_COLLECTION).where("tenderId", "==", tender_id).stream()
    for other in other_bids:
        other_data = other.to_dict()
        if other.id != bid_id and other_data.get("status") == "pending":
            db.collection(BIDS_COLLECTION).document(other.id).update({"status": "rejected"})
            create_notification(
                user_id=other_data.get("agentId"),
                shipment_id=tender_data["shipmentId"],
                message="Your bid was not selected for this shipment.",
                notif_type="bid_rejected",
                tender_id=tender_id,
            )

    tender_ref.update({"status": "closed"})

    shipment_ref = db.collection(SHIPMENTS_COLLECTION).document(tender_data["shipmentId"])
    shipment_ref.update({
        "agentId": bid_data.get("agentId"),
        "currentStage": "assigned",
    })

    create_notification(
        user_id=bid_data.get("agentId"),
        shipment_id=tender_data["shipmentId"],
        message=f"Your bid was accepted! You're now assigned to shipment {tender_data['shipmentId']}.",
        notif_type="bid_accepted",
        tender_id=tender_id,
    )

    return {"id": bid_id, **bid_ref.get().to_dict()}
