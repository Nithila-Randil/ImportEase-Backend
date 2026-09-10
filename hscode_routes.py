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
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "hscode-embeddings")
PINECONE_NAMESPACE  = "__default__"

pc             = Pinecone(api_key=PINECONE_API_KEY)
pinecone_index = pc.Index(PINECONE_INDEX_NAME)


# ============================================================
# HS CODES — all public, no authentication required
# ============================================================

@router.get("/hscodes/search")
def search_hscodes(q: str = Query(..., description="Plain-language product description")):
    """Semantic search over the HS code catalogue.

    Flow:
      1. Pinecone embeds the query with the index's integrated model
         (llama-text-embed-v2) and returns the top-10 nearest records.
      2. Fetch each matching document from Firestore for full detail.
      3. Return the combined list, ordered by similarity score.
    """

    # Step 1 -- Pinecone embeds the text and runs the similarity search
    response = pinecone_index.search(
        namespace=PINECONE_NAMESPACE,
        query={"inputs": {"text": q}, "top_k": 10},
        fields=["description", "category", "heading", "subCategory"],
    )

    hits = response["result"]["hits"]
    if not hits:
        return []

    # Step 2 -- fetch full records from Firestore using the returned IDs
    results = []
    for hit in hits:
        hs_code = hit["id"]
        score   = round(hit["score"], 4)
        fields  = hit.get("fields", {})

        doc = db.collection("hscodes").document(hs_code).get()
        if not doc.exists:
            # Fallback: use the fields stored on the Pinecone record
            results.append({
                "code":               hs_code,
                "description":        fields.get("description", ""),
                "headingDescription": "",
                "category":           fields.get("category", ""),
                "score":              score,
                "source":             "pinecone_only",
            })
            continue

        data = doc.to_dict()
        results.append({
            "code":               data.get("code"),
            "headingDescription": data.get("headingDescription"),
            "category":           data.get("category"),
            "description":        data.get("description"),
            "classificationPath": data.get("classificationPath"),
            "score":              score,
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
    origin: Optional[str] = Query(
        None,
        description="Country of origin (e.g. 'India'). If it qualifies for a "
                    "trade agreement with a lower duty on this code, that rate "
                    "is used and reported in `cidBasis`.",
    ),
):
    """Estimated landed cost = CIF + government duties and taxes only.

    The response carries `isEstimate: true` and a `disclaimer` string — it does
    not include port, handling, clearing-agent, transport or bank charges.
    """
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data      = doc.to_dict()
    breakdown = calculate_landed_cost(data, value, origin=origin)

    return {"code": code, **breakdown}


@router.get("/hscodes/{code}/compliance")
def get_compliance(code: str):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()
    return data.get("compliance", [])