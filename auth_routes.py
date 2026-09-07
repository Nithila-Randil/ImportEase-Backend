from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from firebase_admin import auth
from pydantic import BaseModel

from firebase_setup import db
from dependencies import verify_token

router = APIRouter()


# ============================================================
# Request models
# ============================================================
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str  # "importer" or "clearing_agent"
    agencyCode: Optional[str] = None  # required if role is clearing_agent


class ProfileUpdateRequest(BaseModel):
    phone: Optional[str] = None
    # Importer-specific fields
    isRegisteredBusiness: Optional[bool] = None
    businessName: Optional[str] = None
    businessRegNumber: Optional[str] = None
    businessAddress: Optional[str] = None
    businessCategory: Optional[str] = None
    products: Optional[list[str]] = None
    countries: Optional[list[str]] = None
    importFrequency: Optional[str] = None


# ============================================================
# Helpers (needed by both auth_routes and agency_routes)
# ============================================================
def generate_agency_code():
    """Generate a short, unique-looking agency code, e.g. AG-7X3K9P"""
    import random
    import string
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
# AUTH
# ============================================================
@router.post("/auth/register")
def register(data: RegisterRequest):
    from datetime import datetime, timezone

    if data.role not in ["importer", "clearing_agent"]:
        raise HTTPException(status_code=400, detail="Invalid role")

    agency_id = None
    is_agency_admin = False

    if data.role == "clearing_agent":
        if data.agencyCode:
            agency_id, agency_data = find_agency_by_code(data.agencyCode)
            if not agency_id:
                raise HTTPException(status_code=400, detail="Invalid agency code")
            is_agency_admin = False
        else:
            agency_id = None
            is_agency_admin = True

    # 1. Create the user in Firebase Authentication
    user_record = auth.create_user(
        email=data.email,
        password=data.password,
        display_name=data.name
    )

    # 1b. If independent, auto-create a solo agency
    if data.role == "clearing_agent" and is_agency_admin:
        agency_code = generate_agency_code()
        while find_agency_by_code(agency_code)[0] is not None:
            agency_code = generate_agency_code()

        agency_data = {
            "companyName": data.name,
            "email": data.email,
            "agencyCode": agency_code,
            "adminUid": user_record.uid,
            "profileStatus": "incomplete",
            "businessRegNumber": None,
            "licenseNumber": None,
            "businessAddress": None,
            "businessPhone": None,
            "isIndependent": True,
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


@router.get("/auth/me")
def get_current_user(user: dict = Depends(verify_token)):
    uid = user["uid"]
    doc = db.collection("users").document(uid).get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="User profile not found")

    return {
        "id": uid,
        **doc.to_dict()
    }


@router.put("/users/{id}/profile")
def update_profile(id: str, data: ProfileUpdateRequest, user: dict = Depends(verify_token)):
    if user["uid"] != id:
        raise HTTPException(status_code=403, detail="You can only update your own profile")

    doc_ref = db.collection("users").document(id)
    doc = doc_ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="User not found")

    user_data = doc.to_dict()
    role = user_data.get("role")

    if role == "clearing_agent" and user_data.get("agentStatus") == "rejected":
        raise HTTPException(status_code=403, detail="Rejected agents cannot update their profile")

    # Only touch fields the caller actually sent (exclude_unset), so an
    # omitted field is left alone but an EXPLICIT null really clears a
    # previously-set value -- fixes the "can't clear fields" bug.
    update_data = data.dict(exclude_unset=True)
    update_fields = {}

    if "phone" in update_data:
        update_fields["phone"] = update_data["phone"]

    if role == "clearing_agent":
        final_phone = update_data.get("phone", user_data.get("phone"))
        # Recomputed every time (not just set-and-forget), so clearing the
        # phone later correctly flips this back to false.
        update_fields["profileComplete"] = bool(final_phone)

    elif role == "importer":
        if "isRegisteredBusiness" in update_data:
            update_fields["isRegisteredBusiness"] = update_data["isRegisteredBusiness"]
            if update_data["isRegisteredBusiness"] is False:
                # Switched off -- clear the now-irrelevant business fields
                # instead of leaving stale values sitting in Firestore.
                update_fields["businessName"] = None
                update_fields["businessRegNumber"] = None
                update_fields["businessAddress"] = None
                update_fields["businessCategory"] = None

        is_registered_business = update_data.get("isRegisteredBusiness", user_data.get("isRegisteredBusiness"))
        if is_registered_business:
            for field in ["businessName", "businessRegNumber", "businessAddress", "businessCategory"]:
                if field in update_data:
                    update_fields[field] = update_data[field]

        for field in ["products", "countries", "importFrequency"]:
            if field in update_data:
                update_fields[field] = update_data[field]

        final_phone = update_data.get("phone", user_data.get("phone"))
        update_fields["profileComplete"] = bool(final_phone)

    doc_ref.update(update_fields)

    updated_doc = doc_ref.get()
    return {
        "id": id,
        **updated_doc.to_dict()
    }


@router.delete("/users/{id}")
def delete_user(id: str, user: dict = Depends(verify_token)):
    """Self-service account deletion -- a user can only delete their own account."""
    if user["uid"] != id:
        raise HTTPException(status_code=403, detail="You can only delete your own account")

    doc_ref = db.collection("users").document(id)
    if not doc_ref.get().exists:
        raise HTTPException(status_code=404, detail="User not found")

    doc_ref.delete()
    auth.delete_user(id)

    return {"detail": "Account deleted"}
