"""
Bulk import script — reads the real Sri Lanka Customs HS code data from
HS_CODES.xlsx, extracts genuine product-level codes (skipping section
headings and blank rows), generates Gemini embeddings, and writes
everything to Firestore.

RESUMABLE: since Gemini's free tier caps at 1,000 requests/day and we
have 1,473 codes, this script checks Firestore first and SKIPS any code
that already has an embedding — so you can safely run it again on a
later day to pick up where you left off, without re-spending quota on
codes already done.

Run with: python import_hscodes_from_excel.py
"""

import re
import time
import pandas as pd

from dotenv import load_dotenv
import os
from google import genai

from firebase_setup import db

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

EXCEL_FILE = "HS_CODES.xlsx"
SHEET_NAME = "Table 1"

# Column positions, confirmed against the real file structure
COL_CODE = 3
COL_DESCRIPTION = 10
COL_UNIT = 30
COL_GEN_DUTY = 64
COL_VAT = 68
COL_PAL = 72
COL_CESS = 79
COL_EXCISE = 82
COL_SSCL = 89
COL_SCL = 95

# Safety margin below the real 1,000/day Gemini limit, in case other
# testing also uses the same key on the same day
DAILY_CALL_LIMIT = 900

# Rough chapter-number -> category mapping for common chapters.
# Anything not listed falls back to "Chapter {n}".
CHAPTER_CATEGORIES = {
    1: "Live Animals", 2: "Meat", 3: "Fish & Seafood", 4: "Dairy & Eggs",
    9: "Coffee, Tea, Spices", 10: "Cereals", 17: "Sugar",
    27: "Mineral Fuels", 30: "Pharmaceuticals", 33: "Cosmetics",
    39: "Plastics", 44: "Wood Products", 61: "Textiles (Knitted)",
    62: "Textiles (Woven)", 64: "Footwear", 82: "Tools & Cutlery",
    84: "Machinery", 85: "Electronics", 87: "Vehicles", 94: "Furniture",
}


def get_category(code_str):
    """Derive category from the first 2 digits of the HS code (its chapter)."""
    digits = code_str.replace(".", "")[:2]
    try:
        chapter = int(digits)
        return CHAPTER_CATEGORIES.get(chapter, f"Chapter {chapter}")
    except ValueError:
        return "Uncategorized"


def parse_rate(value):
    """Converts a raw cell value into a decimal rate.
    'Free'/'Ex'/'Con' -> 0.0. Percentages and plain numbers -> decimal.
    Flat 'Rs.X per unit' amounts aren't percentage-based, so they're
    flagged as None — these need manual review, not automatic calculation."""
    if pd.isna(value):
        return 0.0

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().lower()

    if text in ("free", "ex", "ex.", "nil", "-", "con", "conditional"):
        return 0.0

    if "%" in text:
        try:
            return float(text.replace("%", "").strip()) / 100
        except ValueError:
            return 0.0

    if "rs" in text or "per" in text:
        return None  # flat rate — not percentage-based, needs manual handling

    try:
        return float(text)
    except ValueError:
        return 0.0


def extract_codes_from_excel():
    """Reads the Excel file and returns a list of clean HS code dicts,
    skipping section headings, blank rows, and repeated page headers."""
    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME, header=None)

    extracted = []
    skipped_flat_rate = 0

    for _, row in df.iterrows():
        description = row[COL_DESCRIPTION]
        code_raw = row[COL_CODE]

        # A real product row needs both a code AND a description
        if pd.isna(description) or pd.isna(code_raw):
            continue

        code_str = str(code_raw).strip()

        # Skip anything that doesn't look like a real code (e.g. stray text)
        if not re.match(r"^\d", code_str):
            continue

        gen_duty = parse_rate(row[COL_GEN_DUTY])
        vat = parse_rate(row[COL_VAT])
        pal = parse_rate(row[COL_PAL])
        cess = parse_rate(row[COL_CESS])
        sscl = parse_rate(row[COL_SSCL])
        scl = parse_rate(row[COL_SCL])

        # If any rate came back as a flat amount (None), skip for now —
        # these need manual review since they don't fit percentage-based math
        if None in (gen_duty, vat, pal, cess, sscl, scl):
            skipped_flat_rate += 1
            continue

        extracted.append({
            "code": code_str,
            "description": str(description).strip(),
            "category": get_category(code_str),
            "unit": str(row[COL_UNIT]).strip() if pd.notna(row[COL_UNIT]) else None,
            "cid_rate": gen_duty,
            "vat_rate": vat,
            "pal_rate": pal,
            "cess_rate": cess,
            "scl_rate": scl,
            "sscl_rate": sscl,
            "compliance": []
        })

    print(f"Extracted {len(extracted)} usable codes.")
    print(f"Skipped {skipped_flat_rate} codes with flat (non-percentage) rates — needs manual review later.")
    return extracted


def seed_with_resumability(codes):
    """Writes codes to Firestore, generating a Gemini embedding for each.
    Skips any code that already has an embedding stored, so this can be
    safely re-run across multiple days without wasting quota."""
    calls_made_this_run = 0
    newly_seeded = 0
    already_done = 0

    batch = db.batch()
    batch_count = 0

    for item in codes:
        if calls_made_this_run >= DAILY_CALL_LIMIT:
            print(f"\nReached the daily call limit ({DAILY_CALL_LIMIT}). Stopping here.")
            print("Run this script again tomorrow to continue from where you left off.")
            break

        doc_ref = db.collection("hscodes").document(item["code"])
        existing = doc_ref.get()

        if existing.exists and existing.to_dict().get("embedding"):
            already_done += 1
            continue

        try:
            result = client.models.embed_content(
                model="gemini-embedding-001",
                contents=item["description"]
            )
            item["embedding"] = result.embeddings[0].values
        except Exception as e:
            print(f"Failed to embed {item['code']}: {e}")
            continue

        batch.set(doc_ref, item)
        batch_count += 1
        calls_made_this_run += 1
        newly_seeded += 1

        # Firestore batches max out at 100 writes
        if batch_count >= 50:
            batch.commit()
            batch = db.batch()
            batch_count = 0

        if calls_made_this_run % 50 == 0:
            print(f"Progress: {calls_made_this_run} embedded this run...")

        time.sleep(0.1)  # small pacing delay, gentle on rate limits

    if batch_count > 0:
        batch.commit()

    print(f"\nDone for today.")
    print(f"Newly seeded: {newly_seeded}")
    print(f"Already had embeddings (skipped): {already_done}")
    print(f"Remaining (if any): {len(codes) - newly_seeded - already_done}")


if __name__ == "__main__":
    codes = extract_codes_from_excel()
    codes = codes[:700]
    seed_with_resumability(codes)
