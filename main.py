from fastapi import FastAPI

import firebase_setup  # noqa: F401  (import triggers Firebase initialization)

from auth_routes import router as auth_router
from agency_routes import router as agency_router
from hscode_routes import router as hscode_router

app = FastAPI()

app.include_router(auth_router)
app.include_router(agency_router)
app.include_router(hscode_router)


@app.get("/")
def root():
    return {"message": "ImportEase Backend is running!"}
