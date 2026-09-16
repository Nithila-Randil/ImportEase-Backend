"""
notification_routes.py

Handles the Notifications feature for ImportEase:
- List a user's notifications (optionally unread only)
- Mark a notification as read

Firestore collection: "notifications"

Notifications are actually CREATED elsewhere -- in shipment_routes.py (when
a shipment's stage advances) and tender_routes.py (when a bid is accepted)
-- via the create_notification() helper defined in this file and imported
into those files. This file itself only exposes list / mark-as-read.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from firebase_setup import db
from dependencies import verify_token

router = APIRouter()

NOTIFICATIONS_COLLECTION = "notifications"


def create_notification(user_id: str, shipment_id: str, message: str,
                         notif_type: str = "general", tender_id: str = None):
    """Call this from other route files to create a notification for a user.

    `notif_type` + `tenderId` let each role's Notifications page send the
    click on a notification to the right screen (e.g. a "new_bid" notification
    takes an importer straight to that tender) without the frontend having to
    parse the human-readable `message`."""
    if not user_id:
        return None
    doc_ref = db.collection(NOTIFICATIONS_COLLECTION).document()
    data = {
        "userId": user_id,
        "shipmentId": shipment_id,
        "tenderId": tender_id,
        "type": notif_type,
        "message": message,
        "read": False,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    doc_ref.set(data)
    return {"id": doc_ref.id, **data}


@router.get("/notifications")
def list_notifications(unreadOnly: bool = Query(False), user: dict = Depends(verify_token)):
    query = db.collection(NOTIFICATIONS_COLLECTION).where("userId", "==", user["uid"])
    if unreadOnly:
        query = query.where("read", "==", False)
    return [{"id": doc.id, **doc.to_dict()} for doc in query.stream()]


@router.put("/notifications/{notification_id}/read")
def mark_as_read(notification_id: str, user: dict = Depends(verify_token)):
    doc_ref = db.collection(NOTIFICATIONS_COLLECTION).document(notification_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Notification not found")

    data = doc.to_dict()
    if data.get("userId") != user["uid"]:
        raise HTTPException(status_code=403, detail="Not authorized to modify this notification")

    doc_ref.update({"read": True})
    updated = doc_ref.get().to_dict()
    return {"id": notification_id, **updated}
