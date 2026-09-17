"""
message_routes.py

In-app messaging between an importer and the clearing agent assigned to
their shipment. There is no separate "conversation" entity -- a shipment
IS the conversation, scoped to exactly its importer and its assigned agent
(same participants already enforced throughout shipment_routes.py).

Firestore: "shipments/{shipmentId}/messages" (subcollection)
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from firebase_setup import db
from dependencies import verify_token

router = APIRouter()

SHIPMENTS_COLLECTION = "shipments"


class MessageCreate(BaseModel):
    text: str


def _get_conversation_shipment(shipment_id: str, uid: str):
    """Fetch a shipment doc and confirm the caller is its importer or its
    assigned agent, and that an agent has actually been assigned (there's
    no one to message otherwise). Raises 404/403/400 as appropriate."""
    doc_ref = db.collection(SHIPMENTS_COLLECTION).document(shipment_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Shipment not found")

    data = doc.to_dict()
    if data.get("importerId") != uid and data.get("agentId") != uid:
        raise HTTPException(status_code=403, detail="You are not part of this conversation")

    if not data.get("agentId"):
        raise HTTPException(
            status_code=400,
            detail="No clearing agent has been assigned to this shipment yet",
        )

    return doc_ref, data


@router.get("/shipments/{shipment_id}/messages")
def list_messages(shipment_id: str, user: dict = Depends(verify_token)):
    _get_conversation_shipment(shipment_id, user["uid"])

    docs = (
        db.collection(SHIPMENTS_COLLECTION)
        .document(shipment_id)
        .collection("messages")
        .order_by("createdAt")
        .stream()
    )
    return [{"id": doc.id, **doc.to_dict()} for doc in docs]


@router.post("/shipments/{shipment_id}/messages")
def send_message(shipment_id: str, data: MessageCreate, user: dict = Depends(verify_token)):
    text = data.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    shipment_ref, _ = _get_conversation_shipment(shipment_id, user["uid"])

    now = datetime.now(timezone.utc).isoformat()
    message_ref = shipment_ref.collection("messages").document()
    message_data = {
        "senderId": user["uid"],
        "text": text,
        "createdAt": now,
    }
    message_ref.set(message_data)

    # Denormalized onto the shipment so the conversation list (GET /shipments)
    # can show a preview + sort by recency without fetching every shipment's
    # messages just to build that list.
    shipment_ref.update({
        "lastMessage": text,
        "lastMessageAt": now,
    })

    return {"id": message_ref.id, **message_data}
