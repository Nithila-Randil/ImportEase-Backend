import time

import requests
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from dotenv import load_dotenv
import os
from pinecone import Pinecone

from firebase_setup import db
from duty_calculator import calculate_landed_cost, list_preferential_countries

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
         (llama-text-embed-v2) and returns the top-15 nearest records.
      2. Fetch each matching document from Firestore for full detail.
      3. Return the combined list, ordered by similarity score.
    """

    # Step 1 -- Pinecone embeds the text and runs the similarity search
    response = pinecone_index.search(
        namespace=PINECONE_NAMESPACE,
        query={"inputs": {"text": q}, "top_k": 15},
        fields=["description", "chapterTitle", "heading", "subCategory", "chapter"],
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
                "chapterTitle":       fields.get("chapterTitle", ""),
                "chapter":            fields.get("chapter"),
                "score":              score,
                "source":             "pinecone_only",
            })
            continue

        data = doc.to_dict()
        results.append({
            "code":               data.get("code"),
            "headingDescription": data.get("headingDescription"),
            "chapterTitle":       data.get("chapterTitle"),
            "chapter":            data.get("chapter"),
            "description":        data.get("description"),
            "classificationPath": data.get("classificationPath"),
            "score":              score,
        })

    return results


# NOTE: FastAPI matches routes in declaration order, and /hscodes/{code} is a
# single-segment wildcard -- any other single-segment /hscodes/<literal>
# route (like this one) MUST be declared above it, or {code} will swallow it.
@router.get("/hscodes/preferential-countries")
def get_preferential_countries():
    """Countries covered by any Sri Lanka trade agreement, one label each --
    powers the origin-country dropdown. Built from duty_calculator.py's own
    PREFERENTIAL_AGREEMENTS data so the list can never drift out of sync with
    what /hscodes/{code}/landed-cost actually checks."""
    return list_preferential_countries()


@router.get("/hscodes/{code}")
def get_hscode_detail(code: str):
    doc = db.collection("hscodes").document(code).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="HS code not found")

    data = doc.to_dict()
    return {
        "code":               data.get("code"),
        "description":        data.get("description"),
        "headingDescription": data.get("headingDescription"),
        "classificationPath": data.get("classificationPath"),
        "chapterTitle":       data.get("chapterTitle"),
        "chapter":            data.get("chapter"),
        "unit":               data.get("unit"),
        # Import Control License / Sri Lanka Standards Institution marking.
        # Blank means no special control on this code. When present (e.g.
        # "L", "S", "LS", "B") it means SOME kind of import license and/or
        # SLSI certification requirement applies -- we don't have a verified
        # legend for exactly what each letter means, so the frontend shows
        # the raw code with a "verify with Customs" caution rather than
        # guessing at a precise meaning.
        "iclSlsi":            data.get("iclSlsi"),
    }


# Exchange rate -- refreshed at most once every 30 minutes, so a page full of
# calculator visitors doesn't hammer the free external API on every request.
_exchange_rate_cache = {"rate": None, "fetchedAt": 0, "asOf": None}
_EXCHANGE_RATE_TTL_SECONDS = 30 * 60


@router.get("/exchange-rate")
def get_usd_to_lkr_rate():
    """Live USD -> LKR rate, used to convert a CIF value entered in USD into
    the LKR figure Sri Lanka Customs duties are actually assessed on."""
    now = time.time()
    if _exchange_rate_cache["rate"] and now - _exchange_rate_cache["fetchedAt"] < _EXCHANGE_RATE_TTL_SECONDS:
        return {
            "rate": _exchange_rate_cache["rate"],
            "asOf": _exchange_rate_cache["asOf"],
            "cached": True,
        }

    try:
        response = requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        response.raise_for_status()
        data = response.json()
        rate = data["rates"]["LKR"]
        as_of = data.get("time_last_update_utc")
    except Exception:
        if _exchange_rate_cache["rate"]:
            # Serve the last known rate rather than failing outright.
            return {
                "rate": _exchange_rate_cache["rate"],
                "asOf": _exchange_rate_cache["asOf"],
                "cached": True,
                "stale": True,
            }
        raise HTTPException(status_code=503, detail="Could not fetch the exchange rate right now")

    _exchange_rate_cache.update(rate=rate, fetchedAt=now, asOf=as_of)
    return {"rate": rate, "asOf": as_of, "cached": False}


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