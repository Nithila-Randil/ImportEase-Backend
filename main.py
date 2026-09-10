import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import firebase_setup  # noqa: F401  (import triggers Firebase initialization)

from auth_routes import router as auth_router
from agency_routes import router as agency_router
from hscode_routes import router as hscode_router
from shipment_routes import router as shipment_router
from tender_routes import router as tender_router 
from document_routes import router as document_router
from notification_routes import router as notification_router
from agent_rating_routes import router as agent_rating_router

load_dotenv()

app = FastAPI()

# CORS — allow the frontend origin(s) to call the API from a browser.
# Override in .env with CORS_ORIGINS as a comma-separated list, e.g.
#   CORS_ORIGINS=https://app.importease.lk,https://staging.importease.lk
_default_origins = "http://localhost:5173,http://127.0.0.1:5173"
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", _default_origins).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
