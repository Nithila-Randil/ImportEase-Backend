"""
admin_routes.py

ImportEase-staff-only endpoints. Currently: reviewing independent clearing-agent
applications. Independent agents have no agency admin above them, so a platform
admin (a `users` doc with `isPlatformAdmin == True`) approves or rejects them.

Firestore collections touched: "users", "agencies"
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from firebase_setup import db
from dependencies import require_platform_admin

router = APIRouter(prefix="/admin", tags=["admin"])

USERS_COLLECTION = "users"
AGENCIES_COLLECTION = "agencies"


class AgentDecisionRequest(BaseModel):
    decision: str  # "approved" or "rejected"


@router.get("/pending-agents")
def list_pending_independent_agents(admin: dict = Depends(require_platform_admin)):
    """Every independent clearing agent still waiting on platform review."""
    query = (
        db.collection(USERS_COLLECTION)
        .where("role", "==", "clearing_agent")
        .where("isIndependent", "==", True)
        .where("agentStatus", "==", "pending")
        .get()
    )
    return [{"id": doc.id, **doc.to_dict()} for doc in query]


@router.put("/agents/{agent_id}/approve")
def decide_independent_agent(
    agent_id: str,
    data: AgentDecisionRequest,
    admin: dict = Depends(require_platform_admin),
):
    if data.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")

    agent_ref = db.collection(USERS_COLLECTION).document(agent_id)
    agent_doc = agent_ref.get()
    if not agent_doc.exists:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent_data = agent_doc.to_dict()
    if agent_data.get("role") != "clearing_agent" or not agent_data.get("isIndependent"):
        raise HTTPException(status_code=400, detail="This user is not an independent clearing agent")

    agent_ref.update({"agentStatus": data.decision})

    # On approval, activate the agent's solo agency too -- the bidding
    # eligibility gate requires the agency's profileStatus to be "active".
    if data.decision == "approved":
        agency_id = agent_data.get("agencyId")
        if agency_id:
            agency_ref = db.collection(AGENCIES_COLLECTION).document(agency_id)
            if agency_ref.get().exists:
                agency_ref.update({"profileStatus": "active"})

    updated = agent_ref.get()
    return {"id": agent_id, **updated.to_dict()}
