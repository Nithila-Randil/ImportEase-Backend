from fastapi import FastAPI

import firebase_setup  # noqa: F401  (import triggers Firebase initialization)

from auth_routes import router as auth_router
from agency_routes import router as agency_router
from hscode_routes import router as hscode_router
from shipment_routes import router as shipment_router
from tender_routes import router as tender_router 
from document_routes import router as document_router
from notification_routes import router as notification_router
from agent_rating_routes import router as agent_rating_router

app = FastAPI()

app.include_router(auth_router)
app.include_router(agency_router)
app.include_router(hscode_router)
app.include_router(shipment_router)
app.include_router(tender_router)
app.include_router(document_router)
app.include_router(notification_router)
app.include_router(agent_rating_router)


@app.get("/")
def root():
    return {"message": "ImportEase Backend is running!"}
