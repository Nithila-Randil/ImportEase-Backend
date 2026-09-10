from fastapi import Depends, Header, HTTPException
from firebase_admin import auth

from firebase_setup import db


def verify_token(authorization: str = Header(...)):
    """Verifies the Bearer token on protected routes. Returns the decoded
    token (dict with uid, email, etc.) if valid, otherwise raises 401."""
    try:
        token = authorization.split(" ")[1]
        decoded_token = auth.verify_id_token(token)
        return decoded_token
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def require_role(*allowed_roles: str):
    """Dependency factory: use in place of `verify_token` on routes that are
    wholly restricted to one or more roles (e.g. only importers create
    shipments, only clearing agents advance a shipment's stage).

    Looks the caller's role up in the `users` collection and raises 403 if it
    isn't in `allowed_roles`. Keep fine-grained ownership checks (this specific
    importer / the assigned agent) inline in the route.

    Returns the decoded token, same as `verify_token`, plus a `"profile"` key
    holding the caller's `users` document so the route can reuse it without a
    second read.
    """
    allowed = set(allowed_roles)

    def dependency(user: dict = Depends(verify_token)):
        doc = db.collection("users").document(user["uid"]).get()
        if not doc.exists:
            raise HTTPException(status_code=403, detail="User profile not found")

        profile = doc.to_dict()
        if profile.get("role") not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"This action requires role: {' or '.join(sorted(allowed))}",
            )

        return {**user, "profile": profile}

    return dependency
