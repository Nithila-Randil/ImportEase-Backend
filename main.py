import random
import string
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore, auth
from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel

# ============================================================
# Firebase setup
# ============================================================
cred = credentials.Certificate("firebase-key.json")
firebase_admin.initialize_app(cred)
db = firestore.client(database_id="importease")

app = FastAPI()


# ============================================================
# Auth dependency — verifies the Bearer token on protected routes
# ============================================================
def verify_token(authorization: str = Header(...)):
    try:
        token = authorization.split(" ")[1]
        decoded_token = auth.verify_id_token(token)
        return decoded_token  # dict with uid, email, etc.
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ============================================================
# Request models
# ============================================================
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str  # "importer" or "clearing_agent"
    agencyCode: Optional[str] = None  # required if role is clearing_agent


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


class ProfileUpdateRequest(BaseModel):
    phone: Optional[str] = None
    # SME-specific fields
    isRegisteredBusiness: Optional[bool] = None
    businessName: Optional[str] = None
    businessRegNumber: Optional[str] = None
    businessAddress: Optional[str] = None
    businessCategory: Optional[str] = None
    products: Optional[list[str]] = None
    countries: Optional[list[str]] = None
    importFrequency: Optional[str] = None


# ============================================================
# Helpers
# ============================================================
def generate_agency_code():
    """Generate a short, unique-looking agency code, e.g. AG-7X3K9P"""
    chars = string.ascii_uppercase + string.digits
    suffix = "".join(random.choices(chars, k=6))
    return f"AG-{suffix}"


def find_agency_by_code(code: str):
    """Look up an agency document by its agencyCode field. Returns (doc_id, doc_dict) or (None, None)."""
    query = db.collection("agencies").where("agencyCode", "==", code).limit(1).get()
    for doc in query:
        return doc.id, doc.to_dict()
    return None, None


# ============================================================
# Root
# ============================================================
@app.get("/")
def root():
    return {"message": "ImportEase Backend is running!"}


# ============================================================
# AUTH
# ============================================================
@app.post("/auth/register")
def register(data: RegisterRequest):
    if data.role not in ["importer", "clearing_agent"]:
        raise HTTPException(status_code=400, detail="Invalid role")

    agency_id = None
    is_agency_admin = False

    if data.role == "clearing_agent":
        if data.agencyCode:
            # Joining an existing agency — becomes a pending agent
            agency_id, agency_data = find_agency_by_code(data.agencyCode)
            if not agency_id:
                raise HTTPException(status_code=400, detail="Invalid agency code")
            is_agency_admin = False
        else:
            # No agency code given — treat as an independent agent.
            # Auto-create a one-person "agency" so the rest of the system
            # (profileStatus, bidding eligibility gate) works identically.
            agency_id = None  # created after the user record below
            is_agency_admin = True

    # 1. Create the user in Firebase Authentication
    user_record = auth.create_user(
        email=data.email,
        password=data.password,
        display_name=data.name
    )

    # 1b. If this is an independent clearing agent, create their solo agency now
    if data.role == "clearing_agent" and is_agency_admin:
        agency_code = generate_agency_code()
        while find_agency_by_code(agency_code)[0] is not None:
            agency_code = generate_agency_code()

        agency_data = {
            "companyName": data.name,  # solo agent's own name as the "company"
            "email": data.email,
            "agencyCode": agency_code,
            "adminUid": user_record.uid,
            "profileStatus": "incomplete",
            "businessRegNumber": None,
            "licenseNumber": None,
            "businessAddress": None,
            "businessPhone": None,
            "isIndependent": True,  # flags this as a solo agent, not a real multi-person agency
            "createdAt": datetime.now(timezone.utc).isoformat()
        }
        agency_ref = db.collection("agencies").document()
        agency_ref.set(agency_data)
        agency_id = agency_ref.id

    # 2. Build the Firestore profile
    user_data = {
        "name": data.name,
        "email": data.email,
        "role": data.role
    }

    if data.role == "importer":
        user_data["profileComplete"] = False

    if data.role == "clearing_agent":
        user_data["agencyId"] = agency_id
        user_data["isAgencyAdmin"] = is_agency_admin
        # Independent agents are auto-approved (they're their own admin);
        # agents joining an existing agency wait for that agency's admin
        user_data["agentStatus"] = "approved" if is_agency_admin else "pending"
        user_data["profileComplete"] = False
        user_data["phone"] = None

    db.collection("users").document(user_record.uid).set(user_data)

    # 3. Generate a custom token
    custom_token = auth.create_custom_token(user_record.uid)

    return {
        "token": custom_token.decode("utf-8"),
        "user": {
            "id": user_record.uid,
            **user_data
        }
    }


@app.get("/auth/me")
def get_current_user(user: dict = Depends(verify_token)):
    uid = user["uid"]
    doc = db.collection("users").document(uid).get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="User profile not found")

    return {
        "id": uid,
        **doc.to_dict()
    }


@app.put("/users/{id}/profile")
def update_profile(id: str, data: ProfileUpdateRequest, user: dict = Depends(verify_token)):
    # A user can only update their own profile
    if user["uid"] != id:
        raise HTTPException(status_code=403, detail="You can only update your own profile")

    doc_ref = db.collection("users").document(id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = doc.to_dict()
    role = user_data.get("role")

    update_fields = {}

    # Fields shared by both roles
    if data.phone is not None:
        update_fields["phone"] = data.phone

    if role == "clearing_agent":
        # Agent profile completion — just needs a phone number
        if data.phone:
            update_fields["profileComplete"] = True

    elif role == "importer":
        # SME profile completion — name/phone always, business fields conditional
        if data.isRegisteredBusiness is not None:
            update_fields["isRegisteredBusiness"] = data.isRegisteredBusiness

        if data.isRegisteredBusiness:
            if data.businessName is not None:
                update_fields["businessName"] = data.businessName
            if data.businessRegNumber is not None:
                update_fields["businessRegNumber"] = data.businessRegNumber
            if data.businessAddress is not None:
                update_fields["businessAddress"] = data.businessAddress
            if data.businessCategory is not None:
                update_fields["businessCategory"] = data.businessCategory

        # Optional import info — allowed regardless of business toggle
        if data.products is not None:
            update_fields["products"] = data.products
        if data.countries is not None:
            update_fields["countries"] = data.countries
        if data.importFrequency is not None:
            update_fields["importFrequency"] = data.importFrequency

        # Mark profile complete once name + phone are present
        if data.phone:
            update_fields["profileComplete"] = True

    doc_ref.update(update_fields)

    updated_doc = doc_ref.get()
    return {
        "id": id,
        **updated_doc.to_dict()
    }


# ============================================================
# AGENCIES
# ============================================================
@app.post("/agencies/register")
def register_agency(data: AgencyRegisterRequest):
    # 1. Create the admin user in Firebase Authentication
    user_record = auth.create_user(
        email=data.email,
        password=data.password,
        display_name=data.companyName
    )

    # 2. Generate a unique agency code (retry if collision, though extremely unlikely)
    agency_code = generate_agency_code()
    while find_agency_by_code(agency_code)[0] is not None:
        agency_code = generate_agency_code()

    # 3. Create the agency document
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

    # 4. Create the admin's user profile — auto-approved, since they own the agency
    user_data = {
        "name": data.companyName,
        "email": data.email,
        "role": "clearing_agent",
        "agencyId": agency_id,
        "isAgencyAdmin": True,
        "agentStatus": "approved",
        "profileComplete": False,
        "phone": None
    }
    db.collection("users").document(user_record.uid).set(user_data)

    # 5. Generate a login token
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


@app.get("/agencies/{id}")
def get_agency(id: str, user: dict = Depends(verify_token)):
    doc = db.collection("agencies").document(id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Agency not found")

    return {
        "id": id,
        **doc.to_dict()
    }


@app.put("/agencies/{id}/profile")
def complete_agency_profile(id: str, data: AgencyProfileRequest, user: dict = Depends(verify_token)):
    doc_ref = db.collection("agencies").document(id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Agency not found")

    agency_data = doc.to_dict()

    # Only the agency's admin can complete its profile
    if agency_data.get("adminUid") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the agency admin can complete this profile")

    is_independent = agency_data.get("isIndependent", False)

    # businessRegNumber only applies to real, multi-agent agencies — not solo/independent agents
    if not is_independent and not data.businessRegNumber:
        raise HTTPException(status_code=400, detail="businessRegNumber is required for registered agencies")

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


@app.get("/agencies/{id}/pending-agents")
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


@app.put("/agencies/{id}/agents/{agentId}/approve")
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