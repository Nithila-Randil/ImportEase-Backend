"""
document_routes.py

Handles the Documents feature for ImportEase:
- Generate a CUSDEC helper sheet or commercial invoice for a shipment (metadata record)
- Upload an official receipt/document (clearing agent only)
- List and delete documents for a shipment

Firestore collection: "documents"

Known assumptions / gaps (flagging for review):
- No Firebase Storage bucket is configured in firebase_setup.py, so this does
  NOT upload to cloud storage. Uploaded files are saved to a local
  "uploaded_documents/" folder on the server's disk, and that local path is
  stored on the Firestore record as `storagePath` (extra metadata, not part
  of the OpenAPI Document schema, but harmless). Fine for local testing --
  will NOT persist correctly on most cloud hosts (their disks are often
  ephemeral). Wiring up real Firebase Storage is the proper fix before
  deploying anywhere.
- "Generate CUSDEC" / "Generate invoice" don't produce a real PDF -- no PDF
  library is in requirements.txt, and the Document schema itself has no
  field for actual file content or a download URL. So these create a
  Document *metadata* record only, matching the spec exactly as given.
- Access: owning importer OR assigned agent can view/generate/list/delete.
  Uploading specifically is agent-only, per the OpenAPI summary.

IMPORTANT: file upload needs the `python-multipart` package, which is not
currently in requirements.txt. Add it and reinstall before using this file:
    pip install python-multipart
    (and add python-multipart to requirements.txt)
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from firebase_setup import db
from dependencies import verify_token

router = APIRouter()

DOCUMENTS_COLLECTION = "documents"
SHIPMENTS_COLLECTION = "shipments"
UPLOAD_DIR = "uploaded_documents"


# ---------- Helpers ----------

def _get_shipment_with_access(shipment_id: str, user: dict):
    """Fetch a shipment and verify the caller is the owning importer or the
    assigned agent. Raises 404/403 otherwise."""
    doc = db.collection(SHIPMENTS_COLLECTION).document(shipment_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Shipment not found")
    data = doc.to_dict()
    uid = user["uid"]
    if data.get("importerId") != uid and data.get("agentId") != uid:
        raise HTTPException(status_code=403, detail="Not authorized to access this shipment's documents")
    return data


def _create_document_record(shipment_id: str, document_type: str, file_name: str,
                             source: str, verification_status: str,
                             storage_path: Optional[str] = None, uploaded_by: Optional[str] = None):
    doc_ref = db.collection(DOCUMENTS_COLLECTION).document()
    data = {
        "shipmentId": shipment_id,
        "documentType": document_type,
        "fileName": file_name,
        "source": source,
        "verificationStatus": verification_status,
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
        "storagePath": storage_path,
        "uploadedBy": uploaded_by,
    }
    doc_ref.set(data)
    return {"id": doc_ref.id, **data}


# ---------- Endpoints ----------

@router.post("/shipments/{shipment_id}/documents/cusdec")
def generate_cusdec(shipment_id: str, user: dict = Depends(verify_token)):
    """Generates a CUSDEC helper sheet metadata record for the shipment."""
    shipment_data = _get_shipment_with_access(shipment_id, user)
    file_name = f"CUSDEC_{shipment_data.get('reference', shipment_id)}.txt"
    return _create_document_record(
        shipment_id, "cusdec_helper", file_name, source="generated", verification_status="verified"
    )


@router.post("/shipments/{shipment_id}/documents/invoice")
def generate_invoice(shipment_id: str, user: dict = Depends(verify_token)):
    """Generates a commercial invoice metadata record for the shipment."""
    shipment_data = _get_shipment_with_access(shipment_id, user)
    file_name = f"Invoice_{shipment_data.get('reference', shipment_id)}.txt"
    return _create_document_record(
        shipment_id, "commercial_invoice", file_name, source="generated", verification_status="verified"
    )


@router.get("/shipments/{shipment_id}/documents")
def list_documents(shipment_id: str, user: dict = Depends(verify_token)):
    _get_shipment_with_access(shipment_id, user)
    query = db.collection(DOCUMENTS_COLLECTION).where("shipmentId", "==", shipment_id)
    return [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]


@router.post("/shipments/{shipment_id}/documents")
async def upload_document(
    shipment_id: str,
    file: UploadFile = File(...),
    documentType: str = Form(...),
    user: dict = Depends(verify_token),
):
    """Only the assigned agent can upload an official document/receipt."""
    shipment_data = _get_shipment_with_access(shipment_id, user)
    if shipment_data.get("agentId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Only the assigned agent can upload documents")

    os.makedirs(os.path.join(UPLOAD_DIR, shipment_id), exist_ok=True)
    unique_name = f"{uuid.uuid4().hex}_{file.filename}"
    storage_path = os.path.join(UPLOAD_DIR, shipment_id, unique_name)

    contents = await file.read()
    with open(storage_path, "wb") as f:
        f.write(contents)

    return _create_document_record(
        shipment_id, documentType, file.filename,
        source="uploaded", verification_status="pending",
        storage_path=storage_path, uploaded_by=user["uid"],
    )


@router.delete("/shipments/{shipment_id}/documents/{document_id}")
def delete_document(shipment_id: str, document_id: str, user: dict = Depends(verify_token)):
    _get_shipment_with_access(shipment_id, user)

    doc_ref = db.collection(DOCUMENTS_COLLECTION).document(document_id)
    doc = doc_ref.get()
    if not doc.exists or doc.to_dict().get("shipmentId") != shipment_id:
        raise HTTPException(status_code=404, detail="Document not found")

    data = doc.to_dict()
    storage_path = data.get("storagePath")
    if storage_path and os.path.exists(storage_path):
        os.remove(storage_path)

    doc_ref.delete()
    return {"detail": "Document deleted"}
