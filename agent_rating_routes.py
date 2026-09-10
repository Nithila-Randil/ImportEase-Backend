"""
agent_rating_routes.py

Handles Agent Ratings for ImportEase:
- Importer rates the agent after a shipment reaches "cargo_released"
- Anyone can look up an agent's aggregate rating summary

Firestore collection: "ratings"

Known gaps (flagging for review):
- The OpenAPI spec only defines GET /agents/{agentId}/ratings (the summary
  read). There's no endpoint anywhere for actually SUBMITTING a rating, so
  without one this feature could never have any real data. Added
  POST /shipments/{shipment_id}/rating to fill that gap -- flag if a
  different design was already intended here.
- `averageClearanceTimeHours` in the response is left as None. Computing it
  honestly would need a timestamp for when each shipment was actually
  ASSIGNED (not just created), and shipment_routes.py doesn't currently log
  stage-change history anywhere -- only the current stage. Leaving it null
  rather than faking a number.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from firebase_setup import db
from dependencies import verify_token, require_role

router = APIRouter()

RATINGS_COLLECTION = "ratings"
SHIPMENTS_COLLECTION = "shipments"


class RatingCreate(BaseModel):
    rating: int  # 1-5
    comment: Optional[str] = None


@router.post("/shipments/{shipment_id}/rating")
def rate_shipment(shipment_id: str, rating: RatingCreate,
                  user: dict = Depends(require_role("importer"))):
    """Importer rates the agent once the shipment is fully cleared."""
    doc = db.collection(SHIPMENTS_COLLECTION).document(shipment_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Shipment not found")

    data = doc.to_dict()
    if data.get("importerId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the owning importer can rate this shipment")

    if data.get("currentStage") != "cargo_released":
        raise HTTPException(status_code=400, detail="Can only rate a shipment once it's fully cleared (cargo_released)")

    agent_id = data.get("agentId")
    if not agent_id:
        raise HTTPException(status_code=400, detail="This shipment has no assigned agent to rate")

    if not (1 <= rating.rating <= 5):
        raise HTTPException(status_code=400, detail="rating must be between 1 and 5")

    existing = (
        db.collection(RATINGS_COLLECTION)
        .where("shipmentId", "==", shipment_id)
        .limit(1)
        .get()
    )
    if len(existing) > 0:
        raise HTTPException(status_code=400, detail="This shipment has already been rated")

    rating_ref = db.collection(RATINGS_COLLECTION).document()
    rating_data = {
        "agentId": agent_id,
        "importerId": user["uid"],
        "shipmentId": shipment_id,
        "rating": rating.rating,
        "comment": rating.comment,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    rating_ref.set(rating_data)

    return {"id": rating_ref.id, **rating_data}


@router.get("/agents/{agent_id}/ratings")
def get_agent_ratings(agent_id: str, user: dict = Depends(verify_token)):
    """Returns an agent's aggregate rating summary."""
    rating_docs = list(db.collection(RATINGS_COLLECTION).where("agentId", "==", agent_id).stream())
    scores = [d.to_dict().get("rating") for d in rating_docs if d.to_dict().get("rating") is not None]
    average_rating = round(sum(scores) / len(scores), 2) if scores else 0

    completed_docs = (
        db.collection(SHIPMENTS_COLLECTION)
        .where("agentId", "==", agent_id)
        .where("currentStage", "==", "cargo_released")
        .get()
    )
    completed_jobs = len(completed_docs)

    return {
        "agentId": agent_id,
        "averageRating": average_rating,
        "completedJobs": completed_jobs,
        "averageClearanceTimeHours": None,  # not tracked yet -- see file docstring
    }
