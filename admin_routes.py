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


@router.get("/pending-agencies")
def list_pending_agencies(admin: dict = Depends(require_platform_admin)):
    """Real (non-independent) clearing agencies whose profile is complete
    and waiting on platform review before they can go active. An
    independent agent's own solo agency isn't listed here -- its review
    happens as part of approving the agent (see decide_independent_agent
    below), not as a separate agency review."""
    query = (
        db.collection(AGENCIES_COLLECTION)
        .where("isIndependent", "==", False)
        .where("profileStatus", "==", "pending")
        .get()
    )
    return [{"id": doc.id, **doc.to_dict()} for doc in query]


@router.put("/agencies/{agency_id}/approve")
def decide_agency(
    agency_id: str,
    data: AgentDecisionRequest,
    admin: dict = Depends(require_platform_admin),
):
    if data.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")

    agency_ref = db.collection(AGENCIES_COLLECTION).document(agency_id)
    agency_doc = agency_ref.get()
    if not agency_doc.exists:
        raise HTTPException(status_code=404, detail="Agency not found")

    agency_data = agency_doc.to_dict()
    if agency_data.get("isIndependent"):
        raise HTTPException(
            status_code=400,
            detail="Independent agents are reviewed via /admin/agents/{id}/approve, not this endpoint",
        )

    agency_ref.update({"profileStatus": "active" if data.decision == "approved" else "rejected"})

    # The agency's admin account was created with agentStatus "pending" and
    # can't reach their dashboard until this decision lands -- mirror it
    # onto their own agentStatus so RequireAuth lets them (or keeps
    # blocking them) accordingly.
    admin_uid = agency_data.get("adminUid")
    if admin_uid:
        admin_ref = db.collection(USERS_COLLECTION).document(admin_uid)
        if admin_ref.get().exists:
            admin_ref.update({"agentStatus": data.decision})

    updated = agency_ref.get()
    return {"id": agency_id, **updated.to_dict()}


@router.get("/pending-agency-agents")
def list_pending_agency_agents(admin: dict = Depends(require_platform_admin)):
    """Agency-member clearing agents (joined a real agency with a code, not
    independent, not the agency admin) still waiting on platform review --
    a second gate on top of their own agency admin's approval of agentStatus."""
    query = (
        db.collection(USERS_COLLECTION)
        .where("role", "==", "clearing_agent")
        .where("isIndependent", "==", False)
        .where("isAgencyAdmin", "==", False)
        .where("platformStatus", "==", "pending")
        .get()
    )
    results = []
    for doc in query:
        data = doc.to_dict()
        agency_doc = db.collection(AGENCIES_COLLECTION).document(data.get("agencyId")).get()
        agency_name = agency_doc.to_dict().get("companyName") if agency_doc.exists else None
        results.append({"id": doc.id, "agencyName": agency_name, **data})
    return results


@router.put("/agency-agents/{agent_id}/approve")
def decide_agency_agent(
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
    if (
        agent_data.get("role") != "clearing_agent"
        or agent_data.get("isIndependent")
        or agent_data.get("isAgencyAdmin")
    ):
        raise HTTPException(status_code=400, detail="This user is not a reviewable agency-member agent")

    agent_ref.update({"platformStatus": data.decision})

    updated = agent_ref.get()
    return {"id": agent_id, **updated.to_dict()}
