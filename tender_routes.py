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
from dependencies import verify_token
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
def create_tender(shipment_id: str, user: dict = Depends(verify_token)):
    """Importer posts their (draft) shipment to the Clearing Agent Board."""
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

    hs_code = shipment_data.get("hsCode")
    tender_ref = db.collection(TENDERS_COLLECTION).document()
    tender_data = {
        "shipmentId": shipment_id,
        "importerId": shipment_data.get("importerId"),
        "port": shipment_data.get("destination"),
        "hsCode": hs_code,
        "volume": None,  # not tracked on Shipment yet
        "requiredPermits": _get_required_permits(hs_code),
        "status": "open",
        "postedAt": datetime.now(timezone.utc).isoformat(),
    }
    tender_ref.set(tender_data)

    return {"id": tender_ref.id, **tender_data}


@router.get("/tenders")
def list_tenders(status: Optional[str] = None, user: dict = Depends(verify_token)):
    """Clearing Agent Board -- lists tenders. Filter with ?status=open or ?status=closed."""
    query = db.collection(TENDERS_COLLECTION)
    if status:
        query = query.where("status", "==", status)

    return [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]


@router.get("/tenders/{tender_id}")
def get_tender(tender_id: str, user: dict = Depends(verify_token)):
    _, doc = _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    return {"id": doc.id, **doc.to_dict()}


# ---------- Bids ----------

@router.get("/tenders/{tender_id}/bids")
def list_bids(tender_id: str, user: dict = Depends(verify_token)):
    _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    query = db.collection(BIDS_COLLECTION).where("tenderId", "==", tender_id)
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


@router.post("/tenders/{tender_id}/bids")
def submit_bid(tender_id: str, bid: BidCreate, user: dict = Depends(verify_token)):
    tender_ref, tender_doc = _get_or_404(TENDERS_COLLECTION, tender_id, "Tender not found")
    if tender_doc.to_dict().get("status") != "open":
        raise HTTPException(status_code=400, detail="This tender is no longer open")

    _check_agent_can_bid(user)

    bid_ref = db.collection(BIDS_COLLECTION).document()
    bid_data = {
        "tenderId": tender_id,
        "agentId": user["uid"],
        "feeLkr": bid.feeLkr,
        "clearanceTimelineHours": bid.clearanceTimelineHours,
        "notes": bid.notes,
        "agentRating": None,  # Agent Ratings feature not built yet
        "status": "pending",
        "submittedAt": datetime.now(timezone.utc).isoformat(),
    }
    bid_ref.set(bid_data)

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
        if other.id != bid_id and other.to_dict().get("status") == "pending":
            db.collection(BIDS_COLLECTION).document(other.id).update({"status": "rejected"})

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
    )

    return {"id": bid_id, **bid_ref.get().to_dict()}
