from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from firebase_admin import auth
from pydantic import BaseModel

from firebase_setup import db
from dependencies import verify_token
from auth_routes import generate_agency_code, find_agency_by_code

router = APIRouter()


# ============================================================
# Request models
# ============================================================
class AgencyRegisterRequest(BaseModel):
    companyName: str
    email: str
    password: str


class AgencyProfileRequest(BaseModel):
    licenseNumber: str
    businessAddress: str
    businessPhone: str
    businessRegNumber: Optional[str] = None  # only required for real (non-independent) agencies


class AgentApprovalRequest(BaseModel):
    decision: str  # "approved" or "rejected"


# ============================================================
# AGENCIES
# ============================================================
@router.post("/agencies/register")
def register_agency(data: AgencyRegisterRequest):
    try:
        user_record = auth.create_user(
            email=data.email,
            password=data.password,
            display_name=data.companyName
        )
    except auth.EmailAlreadyExistsError:
        raise HTTPException(status_code=400, detail="An account with that email already exists")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    agency_code = generate_agency_code()
    while find_agency_by_code(agency_code)[0] is not None:
        agency_code = generate_agency_code()

    agency_data = {
        "companyName": data.companyName,
        "email": data.email,
        "agencyCode": agency_code,
        "adminUid": user_record.uid,
        "profileStatus": "incomplete",
        "businessRegNumber": None,
        "licenseNumber": None,
        "businessAddress": None,
        "businessPhone": None,
        "isIndependent": False,
        "createdAt": datetime.now(timezone.utc).isoformat()
    }
    agency_ref = db.collection("agencies").document()
    agency_ref.set(agency_data)
    agency_id = agency_ref.id

    user_data = {
        "name": data.companyName,
        "email": data.email,
        "role": "clearing_agent",
        "agencyId": agency_id,
        "isAgencyAdmin": True,
        "isIndependent": False,
        "agentStatus": "approved",
        "profileComplete": False,
        "phone": None
    }
    db.collection("users").document(user_record.uid).set(user_data)

    custom_token = auth.create_custom_token(user_record.uid)

    return {
        "token": custom_token.decode("utf-8"),
        "agency": {
            "id": agency_id,
            **agency_data
        },
        "user": {
            "id": user_record.uid,
            **user_data
        }
    }


@router.get("/agencies")
def list_agencies(user: dict = Depends(verify_token)):
    """Public directory of active agencies/independent agents -- powers the
    SME's "browse agents" screen for sending a direct request. Only safe,
    public-facing fields are returned; license/contact details stay private."""
    query = db.collection("agencies").where("profileStatus", "==", "active")
    agencies = []
    for doc in query.stream():
        data = doc.to_dict()
        agencies.append({
            "id": doc.id,
            "companyName": data.get("companyName"),
            "isIndependent": data.get("isIndependent", False),
            "businessAddress": data.get("businessAddress"),
        })
    return agencies


@router.get("/agencies/{id}")
def get_agency(id: str, user: dict = Depends(verify_token)):
    doc = db.collection("agencies").document(id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Agency not found")

    return {
        "id": id,
        **doc.to_dict()
    }


@router.put("/agencies/{id}/profile")
def complete_agency_profile(id: str, data: AgencyProfileRequest, user: dict = Depends(verify_token)):
    doc_ref = db.collection("agencies").document(id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Agency not found")

    agency_data = doc.to_dict()

    if agency_data.get("adminUid") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the agency admin can complete this profile")

    is_independent = agency_data.get("isIndependent", False)

    if not is_independent and not data.businessRegNumber:
        raise HTTPException(status_code=400, detail="businessRegNumber is required for registered agencies")

    # Guard against blank strings silently marking the profile "active" --
    # every required field must have real content, not just be present.
    required_fields = [data.licenseNumber, data.businessAddress, data.businessPhone]
    if not is_independent:
        required_fields.append(data.businessRegNumber)

    if any(not (f and f.strip()) for f in required_fields):
        raise HTTPException(status_code=400, detail="Required fields cannot be blank")

    update_fields = {
        "licenseNumber": data.licenseNumber,
        "businessAddress": data.businessAddress,
        "businessPhone": data.businessPhone,
        "profileStatus": "active"
    }

    if not is_independent:
        update_fields["businessRegNumber"] = data.businessRegNumber

    doc_ref.update(update_fields)

    updated_doc = doc_ref.get()
    return {
        "id": id,
        **updated_doc.to_dict()
    }


@router.get("/agencies/{id}/pending-agents")
def list_pending_agents(id: str, user: dict = Depends(verify_token)):
    agency_doc = db.collection("agencies").document(id).get()
    if not agency_doc.exists:
        raise HTTPException(status_code=404, detail="Agency not found")

    agency_data = agency_doc.to_dict()
    if agency_data.get("adminUid") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the agency admin can view pending agents")

    query = (
        db.collection("users")
        .where("agencyId", "==", id)
        .where("agentStatus", "==", "pending")
        .get()
    )

    return [{"id": doc.id, **doc.to_dict()} for doc in query]


@router.put("/agencies/{id}/agents/{agentId}/approve")
def approve_agent(id: str, agentId: str, data: AgentApprovalRequest, user: dict = Depends(verify_token)):
    if data.decision not in ["approved", "rejected"]:
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")

    agency_doc = db.collection("agencies").document(id).get()
    if not agency_doc.exists:
        raise HTTPException(status_code=404, detail="Agency not found")

    if agency_doc.to_dict().get("adminUid") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the agency admin can approve agents")

    agent_ref = db.collection("users").document(agentId)
    agent_doc = agent_ref.get()

    if not agent_doc.exists or agent_doc.to_dict().get("agencyId") != id:
        raise HTTPException(status_code=404, detail="Agent not found in this agency")

    agent_ref.update({"agentStatus": data.decision})

    updated_doc = agent_ref.get()
    return {
        "id": agentId,
        **updated_doc.to_dict()
    }


@router.delete("/agencies/{id}/agents/{agentId}")
def remove_agent(id: str, agentId: str, user: dict = Depends(verify_token)):
    """Agency admin removes an existing agent from their agency (deletes their account)."""
    agency_doc = db.collection("agencies").document(id).get()
    if not agency_doc.exists:
        raise HTTPException(status_code=404, detail="Agency not found")

    if agency_doc.to_dict().get("adminUid") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the agency admin can remove an agent")

    if agentId == user["uid"]:
        raise HTTPException(status_code=400, detail="Admins cannot remove themselves this way")

    agent_ref = db.collection("users").document(agentId)
    agent_doc = agent_ref.get()

    if not agent_doc.exists or agent_doc.to_dict().get("agencyId") != id:
        raise HTTPException(status_code=404, detail="Agent not found in this agency")

    agent_ref.delete()
    auth.delete_user(agentId)

    return {"detail": "Agent removed from agency"}
