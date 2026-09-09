from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from dotenv import load_dotenv
import os
from pinecone import Pinecone

from firebase_setup import db
from duty_calculator import calculate_landed_cost

router = APIRouter()

load_dotenv()
PINECONE_API_KEY    = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "importease-hscodes")

EMBED_MODEL = "multilingual-e5-large"

pc             = Pinecone(api_key=PINECONE_API_KEY)
pinecone_index = pc.Index(PINECONE_INDEX_NAME)


# ============================================================
# HS CODES — all public, no authentication required
# ============================================================

@router.get("/hscodes/search")
def search_hscodes(q: str = Query(..., description="Plain-language product description")):
    """Semantic search using Pinecone inference + Pinecone query.

    Flow:
      1. Pinecone embeds the user's query (input_type='query').
      2. Pinecone performs cosine-similarity internally and returns top-10 IDs.
      3. Fetch each matching document from Firestore to get full details.
      4. Return combined result list (with similarity score).
    """

    # Step 1 -- embed the search query using Pinecone inference
    embed_response = pc.inference.embed(
        model=EMBED_MODEL,
        inputs=[q],
        parameters={"input_type": "query", "truncate": "END"},
    )
    query_vector = embed_response[0]["values"]

    # Step 2 -- Pinecone cosine similarity query; returns top-10 matches
    pinecone_response = pinecone_index.query(
        vector=query_vector,
        top_k=10,
        include_metadata=True,
    )

    matches = pinecone_response.get("matches", [])
    if not matches:
        return []

    # Step 3 -- fetch full records from Firestore using the returned IDs
    results = []
    for match in matches:
        hs_code = match["id"]
        score   = match["score"]

        doc = db.collection("hscodes").document(hs_code).get()
        if not doc.exists:
            # Fallback: use metadata stored in Pinecone if Firestore doc missing
            meta = match.get("metadata", {})
            results.append({
                "code":               hs_code,
                "description":        meta.get("description", ""),
                "headingDescription": "",
                "category":           meta.get("category", ""),
                "score":              round(score, 4),
                "source":             "pinecone_only",
            })
            continue

        data = doc.to_dict()
        results.append({
            "code":               data.get("code"),
            "description":        data.get("description"),
            "headingDescription": data.get("headingDescription"),
            "category":           data.get("category"),
            "score":              round(score, 4),
        })

    return results


@router.get("/hscodes/{code}")
def get_hscode_detail(code: str):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()
    return {
        "code":        data.get("code"),
        "description": data.get("description"),
        "category":    data.get("category"),
        "unit":        data.get("unit"),
    }


@router.get("/hscodes/{code}/landed-cost")
def get_landed_cost(
    code: str,
    value: float = Query(..., description="Declared CIF shipment value"),
    origin: Optional[str] = None,
):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data      = doc.to_dict()
    breakdown = calculate_landed_cost(data, value)

    return {"code": code, **breakdown}


@router.get("/hscodes/{code}/compliance")
def get_compliance(code: str):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()
    return data.get("compliance", [])