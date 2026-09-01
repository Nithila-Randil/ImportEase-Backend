from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import numpy as np
from dotenv import load_dotenv
import os
from google import genai

from firebase_setup import db

router = APIRouter()

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)


# ============================================================
# HS CODES — all public, no authentication required
# ============================================================

def cosine_similarity(a, b):
    """Measures how similar two vectors are, from -1 (opposite) to 1 (identical)."""
    a = np.array(a)
    b = np.array(b)
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

@router.get("/hscodes/search")
def search_hscodes(q: str = Query(..., description="Plain-language product description")):
    """Semantic search — embeds the query and compares it against every
    HS code's stored embedding using cosine similarity, so it can match
    everyday words (e.g. "TV") to formal descriptions (e.g. "television
    receivers") even when the exact words don't overlap."""
    all_codes = db.collection("hscodes").get()

    result = client.models.embed_content(model="gemini-embedding-001", contents=q)
    query_embedding = result.embeddings[0].values

    scored_results = []
    for doc in all_codes:
        data = doc.to_dict()
        stored_embedding = data.get("embedding")
        if not stored_embedding:
            continue

        score = cosine_similarity(query_embedding, stored_embedding)
        scored_results.append((score, data))
        print(f"{score:.3f} — {data.get('description')}" )

    scored_results.sort(key=lambda x: x[0], reverse=True)

    top_matches = [
        {"code": data.get("code"), "description": data.get("description")}
        for score, data in scored_results[:10]
        if score > 0.3
    ]

    return top_matches


@router.get("/hscodes/{code}")
def get_hscode_detail(code: str):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()
    return {
        "code": data.get("code"),
        "description": data.get("description"),
        "category": data.get("category"),
        "unit": data.get("unit")
    }


@router.get("/hscodes/{code}/landed-cost")
def get_landed_cost(code: str, value: float = Query(..., description="Declared CIF shipment value"), origin: Optional[str] = None):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()

    cid_rate = data.get("cid_rate", 0)
    vat_rate = data.get("vat_rate", 0)
    pal_rate = data.get("pal_rate", 0)
    cess_rate = data.get("cess_rate", 0)
    scl_rate = data.get("scl_rate", 0)
    sscl_rate = data.get("sscl_rate", 0)
    port_fee_rate = data.get("port_fee_rate", 0.005)  # default 0.5% of CIF

    cif = value

    cid = cif * cid_rate
    pal = cif * pal_rate
    cess = cif * cess_rate
    scl = cif * scl_rate

    # VAT is calculated on CIF + CID + PAL + CESS, per Sri Lanka customs practice
    vat_base = cif + cid + pal + cess
    vat = vat_base * vat_rate

    # SSCL calculated on CIF for simplicity
    sscl = cif * sscl_rate

    port_fees = cif * port_fee_rate

    total_landed_cost = cif + cid + vat + pal + cess + scl + sscl + port_fees

    return {
        "code": code,
        "declaredValue": round(cif, 2),
        "cid": round(cid, 2),
        "vat": round(vat, 2),
        "pal": round(pal, 2),
        "cess": round(cess, 2),
        "scl": round(scl, 2),
        "sscl": round(sscl, 2),
        "portFees": round(port_fees, 2),
        "totalLandedCost": round(total_landed_cost, 2)
    }


@router.get("/hscodes/{code}/compliance")
def get_compliance(code: str):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()
    return data.get("compliance", [])
