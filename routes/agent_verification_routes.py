"""
agent_verification_routes.py

Handles document uploads for independent clearing-agent verification
(clearing license, identity document, professional certificate) submitted at
the end of signup and reviewed by a platform admin (see admin_routes.py)
before the agent's application is approved.

Firestore collection: "agentVerificationDocuments"

Storage caveat (same one document_routes.py carries): no Firebase Storage
bucket is configured in firebase_setup.py, so files are written to a local
"uploaded_documents/agents/" folder on the server's disk and that path is
stored on the Firestore record as `storagePath`. Fine for local testing --
will NOT persist on most cloud hosts (their disks are often ephemeral).
Wiring up real Firebase Storage is the proper fix before deploying anywhere.
"""

import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from firebase_setup import db
from dependencies import verify_token

router = APIRouter()

DOCUMENTS_COLLECTION = "agentVerificationDocuments"
USERS_COLLECTION = "users"
UPLOAD_DIR = os.path.join("uploaded_documents", "agents")

ALLOWED_DOCUMENT_TYPES = {"license", "identity"}


def _is_platform_admin(uid: str) -> bool:
    doc = db.collection(USERS_COLLECTION).document(uid).get()
    return bool(doc.exists and doc.to_dict().get("isPlatformAdmin"))


def _require_self_or_platform_admin(agent_id: str, user: dict):
    if user["uid"] != agent_id and not _is_platform_admin(user["uid"]):
        raise HTTPException(status_code=403, detail="Not authorized to access these documents")


@router.post("/users/{agent_id}/verification-documents")
async def upload_verification_document(
    agent_id: str,
    file: UploadFile = File(...),
    documentType: str = Form(...),
    user: dict = Depends(verify_token),
):
    """An agent uploads one of their own verification documents."""
    if user["uid"] != agent_id:
        raise HTTPException(status_code=403, detail="You can only upload your own verification documents")

    if documentType not in ALLOWED_DOCUMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"documentType must be one of {sorted(ALLOWED_DOCUMENT_TYPES)}",
        )

    agent_doc = db.collection(USERS_COLLECTION).document(agent_id).get()
    if not agent_doc.exists or agent_doc.to_dict().get("role") != "clearing_agent":
        raise HTTPException(status_code=404, detail="Agent not found")

    agent_dir = os.path.join(UPLOAD_DIR, agent_id)
    os.makedirs(agent_dir, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    storage_path = os.path.join(agent_dir, unique_name)

    contents = await file.read()
    with open(storage_path, "wb") as f:
        f.write(contents)

    doc_ref = db.collection(DOCUMENTS_COLLECTION).document()
    data = {
        "userId": agent_id,
        "documentType": documentType,
        "fileName": file.filename,
        "storagePath": storage_path,
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
    }
    doc_ref.set(data)
    return {"id": doc_ref.id, **data}


@router.get("/users/{agent_id}/verification-documents")
def list_verification_documents(agent_id: str, user: dict = Depends(verify_token)):
    """The agent themself, or a platform admin reviewing the application, can list these."""
    _require_self_or_platform_admin(agent_id, user)
    query = db.collection(DOCUMENTS_COLLECTION).where("userId", "==", agent_id)
    return [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]


@router.get("/users/{agent_id}/verification-documents/{document_id}/file")
def download_verification_document(agent_id: str, document_id: str, user: dict = Depends(verify_token)):
    """Streams the raw file back -- used by the admin review page to view/download it."""
    _require_self_or_platform_admin(agent_id, user)

    doc = db.collection(DOCUMENTS_COLLECTION).document(document_id).get()
    if not doc.exists or doc.to_dict().get("userId") != agent_id:
        raise HTTPException(status_code=404, detail="Document not found")

    data = doc.to_dict()
    storage_path = data.get("storagePath")
    if not storage_path or not os.path.exists(storage_path):
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(storage_path, filename=data.get("fileName"))


@router.delete("/users/{agent_id}/verification-documents/{document_id}")
def delete_verification_document(agent_id: str, document_id: str, user: dict = Depends(verify_token)):
    """The agent can remove and re-upload a document while still pending review."""
    if user["uid"] != agent_id:
        raise HTTPException(status_code=403, detail="You can only delete your own verification documents")

    doc_ref = db.collection(DOCUMENTS_COLLECTION).document(document_id)
    doc = doc_ref.get()
    if not doc.exists or doc.to_dict().get("userId") != agent_id:
        raise HTTPException(status_code=404, detail="Document not found")

    storage_path = doc.to_dict().get("storagePath")
    if storage_path and os.path.exists(storage_path):
        os.remove(storage_path)

    doc_ref.delete()
    return {"detail": "Document deleted"}
