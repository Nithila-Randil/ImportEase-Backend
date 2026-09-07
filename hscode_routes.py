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
        print(f"{score:.3f} — {data.get('description')}")

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


def _rate_value(data: dict, field_name: str) -> float:
    """Reads a rate field that may be stored two different ways:
    - Real imported customs data: {"raw": "18%", "type": "percentage", "value": 0.18}
    - Old sample seed data (seed_hscodes.py): a plain number, e.g. 0.18
    Returns the usable decimal rate either way. type "none" (or missing)
    naturally resolves to a value of 0.
    """
    field = data.get(field_name, 0)
    if isinstance(field, dict):
        return field.get("value", 0) or 0
    return field or 0


def _flat_value(data: dict, flat_field_name: str):
    """Looks for a separate flat-amount field (e.g. "cid_flat"). Still a best
    guess -- no real HS code has been seen yet where a duty actually uses the
    'X% OR flat amount, whichever is higher' rule, so this may need
    revisiting once a real example turns up (the real data might encode this
    as a 'type' value like "compound" inside the same rate object instead of
    a separate top-level field)."""
    field = data.get(flat_field_name)
    if isinstance(field, dict):
        return field.get("value")
    return field


@router.get("/hscodes/{code}/landed-cost")
def get_landed_cost(code: str, value: float = Query(..., description="Declared CIF shipment value"), origin: Optional[str] = None):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()

    cid_rate = _rate_value(data, "cid_rate")
    vat_rate = _rate_value(data, "vat_rate")
    pal_rate = _rate_value(data, "pal_rate")
    cess_rate = _rate_value(data, "cess_rate")
    scl_rate = _rate_value(data, "scl_rate")
    sscl_rate = _rate_value(data, "sscl_rate")
    excise_rate = _rate_value(data, "excise_rate")

    port_fee_field = data.get("port_fee_rate", 0.005)
    port_fee_rate = port_fee_field.get("value", 0.005) if isinstance(port_fee_field, dict) else (port_fee_field or 0.005)

    cif = value

    def rate_or_flat(rate, flat_field):
        """Applies an 'X% OR flat amount, whichever is higher' rule when a
        flat amount is defined for this HS code; otherwise falls back to a
        plain percentage."""
        percentage_amount = cif * rate
        flat_amount = _flat_value(data, flat_field)
        if flat_amount:
            return max(percentage_amount, flat_amount)
        return percentage_amount

    scl_applies = scl_rate > 0
    scl = cif * scl_rate

    if scl_applies:
        # SCL REPLACES the CID/VAT/PAL/CESS duty stack rather than adding to
        # it, per Sri Lanka Customs practice for SCL-covered goods.
        cid = 0
        pal = 0
        cess = 0
        vat = 0
    else:
        cid = rate_or_flat(cid_rate, "cid_flat")
        pal = rate_or_flat(pal_rate, "pal_flat")
        cess = rate_or_flat(cess_rate, "cess_flat")
        # VAT is calculated on CIF + CID + PAL + CESS, per Sri Lanka customs practice
        vat_base = cif + cid + pal + cess
        vat = vat_base * vat_rate

    # SSCL, Excise, and port fees sit outside the duty stack, so they still
    # apply either way, regardless of whether SCL kicked in.
    sscl = cif * sscl_rate
    excise = cif * excise_rate
    port_fees = cif * port_fee_rate

    total_landed_cost = cif + cid + vat + pal + cess + scl + sscl + excise + port_fees

    return {
        "code": code,
        "declaredValue": round(cif, 2),
        "cid": round(cid, 2),
        "vat": round(vat, 2),
        "pal": round(pal, 2),
        "cess": round(cess, 2),
        "scl": round(scl, 2),
        "sscl": round(sscl, 2),
        "excise": round(excise, 2),
        "portFees": round(port_fees, 2),
        "totalLandedCost": round(total_landed_cost, 2),
        "sclReplacedDutyStack": scl_applies
    }


@router.get("/hscodes/{code}/compliance")
def get_compliance(code: str):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()
    return data.get("compliance", [])
