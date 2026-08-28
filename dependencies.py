from fastapi import Header, HTTPException
from firebase_admin import auth


def verify_token(authorization: str = Header(...)):
    """Verifies the Bearer token on protected routes. Returns the decoded
    token (dict with uid, email, etc.) if valid, otherwise raises 401."""
    try:
        token = authorization.split(" ")[1]
        decoded_token = auth.verify_id_token(token)
        return decoded_token
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
