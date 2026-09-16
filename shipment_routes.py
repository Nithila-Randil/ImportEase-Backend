"""
shipment_routes.py

Handles the Shipments feature for ImportEase:
- Create / read / update / delete a shipment
- Track a shipment through its 6-stage clearance pipeline

Firestore collection: "shipments"
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from firebase_setup import db
from dependencies import verify_token, require_role
from notification_routes import create_notification

router = APIRouter()

SHIPMENTS_COLLECTION = "shipments"

# The 6 stages, in strict forward order. A shipment can only ever move
# to the NEXT stage in this list -- never skip ahead, never go backward.
STAGE_ORDER = [
    "draft",
    "assigned",
    "cusdec_lodged",
    "duty_paid",
    "inspection",
    "cargo_released",
]


# ---------- Request/response models ----------

class ShipmentCreate(BaseModel):
    origin: str
    destination: str
    # Optional -- an SME can post a clearing request with just the details
    # below even without having picked an HS code from the calculator first.
    hsCode: Optional[str] = None
    declaredValue: float
    description: str
    # Contact + timing details collected on the "Find a clearing agent" request
    # form. All optional so existing callers (and any future ones that don't
    # need them) keep working unchanged.
    estimatedArrival: Optional[str] = None  # ISO date -- when the cargo is expected to arrive
    mustReleaseBy: Optional[str] = None  # ISO date -- latest acceptable release date
    contactPhone: Optional[str] = None
    contactEmail: Optional[str] = None


class ShipmentUpdate(BaseModel):
    origin: Optional[str] = None
    destination: Optional[str] = None
    declaredValue: Optional[float] = None
    description: Optional[str] = None
    estimatedArrival: Optional[str] = None
    mustReleaseBy: Optional[str] = None
    contactPhone: Optional[str] = None
    contactEmail: Optional[str] = None


class StatusUpdate(BaseModel):
    newStage: str


# ---------- Helpers ----------

def _get_shipment_or_404(shipment_id: str):
    """Fetch a shipment doc by id, or raise 404 if it doesn't exist."""
    doc_ref = db.collection(SHIPMENTS_COLLECTION).document(shipment_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Shipment not found")
    return doc_ref, doc


# ---------- Endpoints ----------

@router.post("/shipments")
def create_shipment(shipment: ShipmentCreate, user: dict = Depends(require_role("importer"))):
    """Only an importer can create a shipment."""
    doc_ref = db.collection(SHIPMENTS_COLLECTION).document()

    data = {
        "origin": shipment.origin,
        "destination": shipment.destination,
        "hsCode": shipment.hsCode,
        "declaredValue": shipment.declaredValue,
        "description": shipment.description,
        "estimatedArrival": shipment.estimatedArrival,
        "mustReleaseBy": shipment.mustReleaseBy,
        "contactPhone": shipment.contactPhone,
        "contactEmail": shipment.contactEmail,
        "importerId": user["uid"],
        # Denormalized so the assigned agent's Shipments page can show who
        # they're working for without a per-card importer lookup.
        "importerName": user["profile"].get("name"),
        "agentId": None,
        "currentStage": STAGE_ORDER[0],
        "reference": f"IE-{doc_ref.id[:8].upper()}",
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    doc_ref.set(data)

    created = doc_ref.get().to_dict()
    return {"id": doc_ref.id, **created}


@router.get("/shipments")
def list_shipments(user: dict = Depends(verify_token)):
    """
    Importers see shipments they created.
    Agents see shipments assigned to them.
    """
    uid = user["uid"]
    seen_ids = set()
    results = []

    for doc in db.collection(SHIPMENTS_COLLECTION).where("importerId", "==", uid).stream():
        results.append({"id": doc.id, **doc.to_dict()})
        seen_ids.add(doc.id)

    for doc in db.collection(SHIPMENTS_COLLECTION).where("agentId", "==", uid).stream():
        if doc.id not in seen_ids:
            results.append({"id": doc.id, **doc.to_dict()})

    return results


@router.get("/shipments/{shipment_id}")
def get_shipment(shipment_id: str, user: dict = Depends(verify_token)):
    """Only the owning importer or the assigned agent can view a shipment."""
    _, doc = _get_shipment_or_404(shipment_id)
    data = doc.to_dict()
    uid = user["uid"]

    if data.get("importerId") != uid and data.get("agentId") != uid:
        raise HTTPException(status_code=403, detail="Not authorized to view this shipment")

    return {"id": doc.id, **data}


@router.put("/shipments/{shipment_id}")
def update_shipment(shipment_id: str, updates: ShipmentUpdate, user: dict = Depends(verify_token)):
    """Only the owning importer can edit origin/destination/declaredValue/description."""
    doc_ref, doc = _get_shipment_or_404(shipment_id)
    data = doc.to_dict()

    if data.get("importerId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the owning importer can update this shipment")

    update_data = updates.dict(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    doc_ref.update(update_data)
    updated = doc_ref.get().to_dict()
    return {"id": shipment_id, **updated}


@router.delete("/shipments/{shipment_id}")
def delete_shipment(shipment_id: str, user: dict = Depends(verify_token)):
    """Only the owning importer can delete a shipment."""
    doc_ref, doc = _get_shipment_or_404(shipment_id)

    if doc.to_dict().get("importerId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the owning importer can delete this shipment")

    doc_ref.delete()
    return {"detail": "Shipment deleted"}


@router.get("/shipments/{shipment_id}/status")
def get_shipment_status(shipment_id: str, user: dict = Depends(verify_token)):
    """Only the owning importer or the assigned agent can check status."""
    _, doc = _get_shipment_or_404(shipment_id)
    data = doc.to_dict()
    uid = user["uid"]

    if data.get("importerId") != uid and data.get("agentId") != uid:
        raise HTTPException(status_code=403, detail="Not authorized to view this shipment's status")

    return {"currentStage": data.get("currentStage")}


@router.put("/shipments/{shipment_id}/status")
def update_shipment_status(shipment_id: str, status_update: StatusUpdate,
                           user: dict = Depends(require_role("clearing_agent"))):
    """
    Only the ASSIGNED AGENT can advance a shipment's stage -- never the importer.
    The new stage must be exactly the next one in STAGE_ORDER: no skipping, no going backward.
    """
    doc_ref, doc = _get_shipment_or_404(shipment_id)
    data = doc.to_dict()

    if data.get("agentId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the assigned agent can update the shipment status")

    current_stage = data.get("currentStage")
    try:
        current_index = STAGE_ORDER.index(current_stage)
    except ValueError:
        raise HTTPException(status_code=500, detail="Shipment has an invalid current stage")

    if current_index == len(STAGE_ORDER) - 1:
        raise HTTPException(status_code=400, detail="Shipment is already at its final stage")

    expected_next = STAGE_ORDER[current_index + 1]
    if status_update.newStage != expected_next:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot move from '{current_stage}' to '{status_update.newStage}'. "
                   f"Next allowed stage is '{expected_next}'.",
        )

    doc_ref.update({"currentStage": expected_next})

    create_notification(
        user_id=data.get("importerId"),
        shipment_id=shipment_id,
        message=f"Your shipment {data.get('reference', shipment_id)} has moved to '{expected_next}'.",
        notif_type="stage_update",
    )

    return {"currentStage": expected_next}
