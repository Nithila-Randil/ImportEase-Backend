"""
Bulk import script — reads real Sri Lanka Customs HS code data directly
from individual chapter PDF files, extracts genuine product-level codes,
stores structured data in Firestore (NO embeddings), and uses Pinecone's
built-in inference API to generate embeddings and upsert vectors.

No Gemini API calls needed — Pinecone handles all embedding internally.

RESUMABLE: checks Pinecone first and SKIPS any code that already has a
vector — safe to re-run.

Setup:
  1. Put all chapter PDF files into a folder named "tariff_pdfs".
  2. Set PINECONE_API_KEY and PINECONE_INDEX_NAME in your .env file.

Run with: python import_hscodes_from_pdf.py
"""

import re
import time
import glob
import pdfplumber

from dotenv import load_dotenv
import os
from pinecone import Pinecone, ServerlessSpec

from firebase_setup import db

load_dotenv()

PINECONE_API_KEY    = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "importease-hscodes")

# ── Pinecone setup ────────────────────────────────────────────────────────────
pc = Pinecone(api_key=PINECONE_API_KEY)

# Pinecone's "multilingual-e5-large" model produces 1024-dim vectors
EMBED_MODEL       = "multilingual-e5-large"
EMBEDDING_DIM     = 1024

if PINECONE_INDEX_NAME not in [idx.name for idx in pc.list_indexes()]:
    print(f"Creating Pinecone index '{PINECONE_INDEX_NAME}' ...")
    pc.create_index(
        name=PINECONE_INDEX_NAME,
        dimension=EMBEDDING_DIM,
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region="us-east-1"),
    )
    while not pc.describe_index(PINECONE_INDEX_NAME).status["ready"]:
        time.sleep(1)
    print("Index ready.")

pinecone_index = pc.Index(PINECONE_INDEX_NAME)

# ─────────────────────────────────────────────────────────────────────────────

PDF_FOLDER      = "tariff_pdfs"
MAX_TOTAL_CODES = 50
UPSERT_BATCH    = 50   # upsert to Pinecone in batches

# ── Dynamic Column Detection per PDF ──────────────────────────────────────────
def detect_columns_from_pdf(pdf):
    """
    Scans pages in the PDF to find the table header row containing 'HS Code'
    and maps the exact column positions for this specific file dynamically.
    """
    # Fallback defaults in case a header row is not found
    cols = {
        "heading": 0,
        "code": 1,
        "description": 3,
        "unit": 4,
        "gen_duty": 16,
        "vat": 17,
        "cess": 20,
        "excise": 21,
        "sscl": 22,
        "scl": 23,
    }

    for page in pdf.pages:
        tables = page.extract_tables()
        if not tables:
            continue

        for table in tables:
            for row in table:
                cleaned = [re.sub(r"\s+", " ", str(c or "")).strip().lower() for c in row]
                if any("hs code" in c for c in cleaned):
                    detected = {
                        "heading": None,
                        "code": None,
                        "description": None,
                        "unit": None,
                        "gen_duty": None,
                        "vat": None,
                        "cess": None,
                        "excise": None,
                        "sscl": None,
                        "scl": None,
                    }
                    for idx, text in enumerate(cleaned):
                        if "hdg" in text or "heading" in text:
                            detected["heading"] = idx
                        elif "hs code" in text:
                            detected["code"] = idx
                        elif "desc" in text:
                            detected["description"] = idx
                        elif re.search(r"\bu\s*n\s*i\s*t\b", text):
                            detected["unit"] = idx
                        elif "gen" in text and "duty" in text:
                            detected["gen_duty"] = idx
                        elif text == "vat":
                            detected["vat"] = idx
                        elif text == "cess":
                            detected["cess"] = idx
                        elif "excise" in text:
                            detected["excise"] = idx
                        elif "sscl" in text:
                            detected["sscl"] = idx
                        elif text in ["s c l", "scl", "l c s"] or "special commodity" in text:
                            detected["scl"] = idx

                    if detected["code"] is not None and detected["description"] is not None:
                        return detected

    return cols


def get_chapter_title_from_pdf(pdf_path):
    """Extracts the real chapter title directly from the PDF's first page,
    e.g. 'Chapter 27' followed by 'Mineral fuels, mineral oils...' --
    matches the actual government document instead of a hand-typed guess."""
    with pdfplumber.open(pdf_path) as pdf:
        first_page_text = pdf.pages[0].extract_text()

    match = re.search(r"Chapter\s+(\d+)\s*\n(.+)", first_page_text)
    if match:
        chapter_number = int(match.group(1))
        title = match.group(2).strip()
        return chapter_number, title

    return None, "Uncategorized"


def parse_rate(value):
    """Detects 'X% or Rs.Y per unit' patterns and returns a structured dict.
    Every result includes 'raw' -- the exact original text from the PDF."""
    if value is None or value.strip() == "":
        return {"type": "none", "value": 0.0, "raw": None}

    raw_original = value.strip()
    text = re.sub(r"\s+", " ", raw_original).lower()

    if text in ("free", "ex", "ex.", "nil", "-", "con", "conditional"):
        return {"type": "none", "value": 0.0, "raw": raw_original}

    or_match = re.search(
        r"(\d+(?:\.\d+)?)\s*%\s*or\s*rs\.?\s*(\d+(?:\.\d+)?)", text
    )
    if or_match:
        return {
            "type": "whichever_higher",
            "percentage": float(or_match.group(1)) / 100,
            "flatAmount": float(or_match.group(2)),
            "raw": raw_original,
        }

    percent_match = re.match(r"^(\d+(?:\.\d+)?)\s*%$", text)
    if percent_match:
        return {
            "type": "percentage",
            "value": float(percent_match.group(1)) / 100,
            "raw": raw_original,
        }

    flat_match = re.search(r"rs\.?\s*(\d+(?:\.\d+)?)", text)
    if flat_match:
        return {
            "type": "flat",
            "flatAmount": float(flat_match.group(1)),
            "raw": raw_original,
        }

    return {"type": "unknown", "raw": raw_original}


def extract_codes_from_pdf(pdf_path):
    """Extracts all valid product-level HS codes from one PDF file,
    dynamically detecting the column layout for this specific file."""
    extracted = []
    chapter_number, category = get_chapter_title_from_pdf(pdf_path)

    current_heading = None
    current_heading_description = None

    def get_cell(row, col_idx):
        """Safely gets text from a row at col_idx if column exists."""
        if col_idx is not None and col_idx < len(row) and row[col_idx] is not None:
            val = str(row[col_idx]).strip()
            return val if val else None
        return None

    with pdfplumber.open(pdf_path) as pdf:
        cols = detect_columns_from_pdf(pdf)
        col_code = cols.get("code")
        col_desc = cols.get("description")
        col_hdg  = cols.get("heading")

        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue

            for row in tables[0]:
                if col_code is None or col_desc is None:
                    continue
                if len(row) <= max(col_code, col_desc):
                    continue

                heading_raw = get_cell(row, col_hdg)
                code_raw    = get_cell(row, col_code)
                description = get_cell(row, col_desc)

                # A heading row (e.g. "27.11") has a heading value but no code
                if heading_raw and not code_raw:
                    current_heading = heading_raw
                    current_heading_description = (
                        description.replace("\n", " ").strip() if description else None
                    )
                    continue

                if not code_raw or not description:
                    continue

                if not re.match(r"^\d{2,4}\.\d{2}", code_raw):
                    continue  # skip section headings, blank rows

                description_clean = description.replace("\n", " ").strip()

                extracted.append({
                    "code":               code_raw,
                    "description":        description_clean,
                    "heading":            current_heading,
                    "headingDescription": current_heading_description,
                    "category":           category,
                    "unit":               get_cell(row, cols.get("unit")),
                    "cid_rate":           parse_rate(get_cell(row, cols.get("gen_duty"))),
                    "vat_rate":           parse_rate(get_cell(row, cols.get("vat"))),
                    "cess_rate":          parse_rate(get_cell(row, cols.get("cess"))),
                    "excise_rate":        parse_rate(get_cell(row, cols.get("excise"))),
                    "sscl_rate":          parse_rate(get_cell(row, cols.get("sscl"))),
                    "scl_rate":           parse_rate(get_cell(row, cols.get("scl"))),
                    "compliance":         [],
                    "sourceFile":         os.path.basename(pdf_path),
                })

    return extracted


def already_in_pinecone(codes_batch):
    """Returns a set of HS code strings that already have vectors in Pinecone."""
    ids = [item["code"] for item in codes_batch]
    try:
        response = pinecone_index.fetch(ids=ids)
        return set(response.vectors.keys())
    except Exception:
        return set()


def embed_texts(texts, input_type="passage"):
    """
    Call Pinecone's inference API to embed a list of strings.
    input_type = "passage"  when indexing documents
    input_type = "query"    when embedding a search query
    Returns a list of float vectors.
    """
    response = pc.inference.embed(
        model=EMBED_MODEL,
        inputs=texts,
        parameters={"input_type": input_type, "truncate": "END"},
    )
    return [item["values"] for item in response]


def seed_with_resumability(codes):
    """
    Two-phase pipeline per HS code:
      1. Write structured data (NO embedding) to Firestore.
      2. Embed with Pinecone inference -> upsert vectors to Pinecone.

    Processes in batches of UPSERT_BATCH for efficiency.
    Already-completed codes (present in Pinecone) are skipped on re-runs.
    """
    newly_seeded = 0
    already_done = 0

    # Pre-fetch which codes are already in Pinecone (checked in chunks of 50)
    already_done_ids = set()
    for i in range(0, len(codes), 50):
        already_done_ids |= already_in_pinecone(codes[i : i + 50])

    print(f"{len(already_done_ids)} codes already in Pinecone -- will skip them.")

    # Filter to only codes that need embedding
    pending = [item for item in codes if item["code"] not in already_done_ids]
    already_done = len(codes) - len(pending)

    # -- Phase 1: Write ALL codes to Firestore (data only, no embedding) -----
    print(f"\nWriting {len(codes)} codes to Firestore ...")
    for item in codes:
        db.collection("hscodes").document(item["code"]).set(item)
    print("Firestore write complete.")

    # -- Phase 2: Embed & upsert in batches to Pinecone ----------------------
    print(f"\nEmbedding and upserting {len(pending)} codes to Pinecone ...")
    for batch_start in range(0, len(pending), UPSERT_BATCH):
        batch = pending[batch_start : batch_start + UPSERT_BATCH]

        # Build the text to embed: description | headingDescription | category
        texts = []
        for item in batch:
            parts = [item["description"]]
            if item.get("headingDescription"):
                parts.append(item["headingDescription"])
            if item.get("category"):
                parts.append(item["category"])
            texts.append(" | ".join(parts))

        try:
            vectors = embed_texts(texts, input_type="passage")
        except Exception as e:
            print(f"Pinecone embed failed for batch starting at {batch_start}: {e}")
            continue

        upsert_payload = [
            {
                "id": batch[i]["code"],
                "values": vectors[i],
                "metadata": {
                    "description": batch[i]["description"],
                    "category":    batch[i].get("category", ""),
                    "heading":     batch[i].get("heading", ""),
                },
            }
            for i in range(len(batch))
        ]

        pinecone_index.upsert(vectors=upsert_payload)
        newly_seeded += len(batch)
        print(f"  Upserted {newly_seeded}/{len(pending)} vectors ...")

        time.sleep(0.1)

    print(f"\nDone.")
    print(f"Newly embedded + upserted to Pinecone: {newly_seeded}")
    print(f"Already had vectors (skipped):          {already_done}")


if __name__ == "__main__":
    pdf_files = glob.glob(os.path.join(PDF_FOLDER, "*.pdf"))
    print(f"Found {len(pdf_files)} PDF files in '{PDF_FOLDER}'.")

    all_codes = []
    for pdf_path in pdf_files:
        if len(all_codes) >= MAX_TOTAL_CODES:
            break
        codes     = extract_codes_from_pdf(pdf_path)
        remaining = MAX_TOTAL_CODES - len(all_codes)
        codes     = codes[:remaining]
        print(f"  {os.path.basename(pdf_path)}: {len(codes)} codes extracted")
        all_codes.extend(codes)

    print(f"\nTotal codes extracted: {len(all_codes)} (limit: {MAX_TOTAL_CODES})")
    seed_with_resumability(all_codes)