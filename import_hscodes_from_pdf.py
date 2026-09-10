"""
Bulk import script — reads real Sri Lanka Customs HS code data directly
from individual chapter PDF files, extracts genuine product-level codes,
stores structured data in Firestore (NO embeddings), and upserts one text
record per code to a Pinecone index whose integrated model
(llama-text-embed-v2) embeds it server-side.

No Gemini / OpenAI calls needed — Pinecone handles all embedding internally.

RESUMABLE: checks Pinecone first and SKIPS any code that already has a
vector — safe to re-run.

Setup:
  1. Put all chapter PDF files into a folder named "tariff_pdfs".
  2. Set PINECONE_API_KEY and PINECONE_INDEX_NAME in your .env file.

Run with:
  python import_hscodes_from_pdf.py            # full import (Firestore + Pinecone)
  python import_hscodes_from_pdf.py --dry-run  # parse only, print stats, no writes


How the extraction works
────────────────────────
The chapter PDFs are born-digital and every tariff table is drawn with real
ruling lines, so pdfplumber's "lines" table strategy reconstructs the grid
(including empty cells) reliably.  The column *meaning* still varies chapter to
chapter — some chapters have an Excise column, some a "Surcharge on Customs
Duty" column, Chapter 87 additionally has Luxury Tax — so we read each table's
own two-row header ("HS Code / Description / … / Gen Duty / VAT / PAL / Cess /
…" on top, "AP AD BN … / Gen SG" underneath) and build a column map from the
labels rather than assuming fixed indices.

Every national duty that appears in the source is captured: CID (Gen Duty),
VAT, PAL, Cess, Excise, Surcharge on Customs Duty, SSCL, SCL and — for
Chapter 87 — Luxury Tax, plus the ten preferential-duty (trade-agreement)
columns.  Six-digit parent headings that only exist to group their eight/
ten-digit children are dropped so they are not stored as fake products.
"""

import re
import sys
import time
import glob
import os

import pdfplumber
from dotenv import load_dotenv

from firebase_setup import db

load_dotenv()

PINECONE_API_KEY    = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "hscode-embeddings")

# The index carries integrated embedding — we upsert plain text and Pinecone
# embeds it server-side with this model. Used only when creating the index.
PINECONE_MODEL     = "llama-text-embed-v2"   # 1024-dim, 2048-token input
PINECONE_NAMESPACE = "__default__"

PDF_FOLDER      = "tariff_pdfs"
MAX_TOTAL_CODES = 5   # how many codes to process; None = all of them
UPSERT_BATCH    = 96     # Pinecone embeds at most 96 records per upsert_records call

# pdfplumber table settings — these tables are fully ruled, so use the lines.
TABLE_SETTINGS = {
    "vertical_strategy":   "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance":      3,
    "join_tolerance":      3,
}

# The ten preferential-duty (bilateral / regional trade-agreement) columns,
# in the fixed order they appear in every chapter.
PREF_COUNTRIES = ["AP", "AD", "BN", "GT", "IN", "PK", "SA", "SF", "SD", "SG"]


# ── Text helpers ─────────────────────────────────────────────────────────────
def _norm(cell):
    """Lower-cased, whitespace-collapsed version of a raw table cell."""
    return re.sub(r"\s+", " ", str(cell or "")).strip().lower()


def _clean(cell):
    """Single-line, trimmed text for storage; None when empty."""
    if cell is None:
        return None
    text = re.sub(r"\s+", " ", str(cell).replace("\n", " ")).strip()
    return text or None


# ── Rate parsing ─────────────────────────────────────────────────────────────
_NONE_TOKENS = {
    "", "-", "--", "free", "ex", "ex.", "nil", "n/a", "na",
    "con", "conditional", "exempt", "exempted",
}

_PER_UNIT_RE = re.compile(
    r"per\s+(unit|pair|kwh?|kgm?|litre|liter|ltr|sqm|piece|pcs?|dozen|gram|gm?|"
    r"mt|ton(?:ne)?|carat|no|nos|m)\b"
)


def _money(num_str, magnitude=None):
    amount = float(num_str.replace(",", "").replace(" ", ""))
    if magnitude in ("mn", "m", "million"):
        amount *= 1_000_000
    elif magnitude in ("bn", "billion"):
        amount *= 1_000_000_000
    return amount


def parse_rate(value):
    """Turn a raw duty-cell string into a structured rate.

    Handles the shapes that actually occur in the tariff:
      "18%"                         -> percentage
      "20.0%"                       -> percentage
      "300%"                        -> percentage (yes, > 100 % happens)
      "Rs.50/= per unit"            -> flat, perUnit="unit"
      "Rs.7,243,550/- per unit"     -> flat (thousands separators, /- suffix)
      "Rs 5.0 Mn"                   -> flat 5_000_000
      "12% or Rs.160/= per unit"    -> whichever_higher (percentage + flat)
      "Free" / "Ex" / "-" / ""      -> none
    Every result keeps 'raw' — the exact original text.
    """
    if value is None:
        return {"type": "none", "value": 0.0, "raw": None}

    raw = str(value).strip()
    text = re.sub(r"\s+", " ", raw).lower().replace("/=", "").replace("/-", "")
    # PDF text extraction sometimes splits thousands groups ("2,25 0,000"),
    # so drop any comma/space that sits between two digits.
    text = re.sub(r"(?<=\d)[ ,](?=\d)", "", text)

    if text in _NONE_TOKENS:
        return {"type": "none", "value": 0.0, "raw": raw}

    per_match = _PER_UNIT_RE.search(text)
    per_unit = per_match.group(1) if per_match else None

    # "X% or Rs.Y ..."  → whichever is higher
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*%\s*or\s*rs\.?\s*([\d,]+(?:\.\d+)?)\s*(mn|m|million|bn|billion)?",
        text,
    )
    if m:
        return {
            "type": "whichever_higher",
            "percentage": float(m.group(1)) / 100,
            "flatAmount": _money(m.group(2), m.group(3)),
            "perUnit": per_unit,
            "raw": raw,
        }

    # "Rs.Y ..." (also "Rs 5.0 Mn")
    m = re.search(r"rs\.?\s*([\d,]+(?:\.\d+)?)\s*(mn|m|million|bn|billion)?", text)
    if m:
        return {
            "type": "flat",
            "flatAmount": _money(m.group(1), m.group(2)),
            "perUnit": per_unit,
            "raw": raw,
        }

    # plain percentage anywhere in the cell
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return {"type": "percentage", "value": float(m.group(1)) / 100, "raw": raw}

    return {"type": "unknown", "raw": raw}


# ── Chapter title ────────────────────────────────────────────────────────────
def get_chapter_title_from_pdf(pdf_path):
    """Reads 'Chapter NN' + the title line(s) from the PDF's first page so the
    category matches the real government document, not a hand-typed guess."""
    with pdfplumber.open(pdf_path) as pdf:
        first_page_text = pdf.pages[0].extract_text() or ""

    match = re.search(
        r"Chapter\s+(\d+)\s*\n+([^\n]+(?:\n[^\n]+){0,2})", first_page_text
    )
    if not match:
        return None, "Uncategorized"

    chapter_number = int(match.group(1))
    title = re.sub(r"\s+", " ", match.group(2)).strip()
    # Drop anything from a "Note." / "Notes." onwards if it bled into the capture
    title = re.split(r"\bnotes?\.", title, flags=re.IGNORECASE)[0].strip(" .,;")
    return chapter_number, title


# ── Column map (per table header) ────────────────────────────────────────────
def build_column_map(table):
    """Find the two-row header inside one extracted table and map every column
    we care about to its index.  Returns None if this table has no header
    (a continuation table on a later page)."""
    label_row = None
    sub_row = None
    for i, row in enumerate(table):
        cells = [_norm(c) for c in row]
        if any("hs code" in c for c in cells):
            label_row = cells
            sub_row = [_norm(c) for c in table[i + 1]] if i + 1 < len(table) else []
            break
    if label_row is None:
        return None

    def find(pred):
        for idx, text in enumerate(label_row):
            if text and pred(text):
                return idx
        return None

    m = {
        "heading":     find(lambda c: "hdg" in c or c == "heading"),
        "code":        find(lambda c: "hs code" in c),
        "description": find(lambda c: c.startswith("desc")),
        "unit":        find(lambda c: c == "unit" or re.fullmatch(r"un\s*it", c)),
        "icl_slsi":    find(lambda c: "icl" in c),
        "gen_duty":    find(lambda c: "gen" in c and "duty" in c),
        "vat":         find(lambda c: c == "vat"),
        "pal":         find(lambda c: c == "pal"),
        "cess":        find(lambda c: c == "cess"),
        "excise":      find(lambda c: "excise" in c),
        "scd":         find(lambda c: "surcharge" in c),  # Surcharge on Customs Duty
        "sscl":        find(lambda c: "sscl" in c),
        "scl":         find(lambda c: c in ("s c l", "l c s", "scl", "lcs")
                                      or "special commodity" in c),
        "luxury":      find(lambda c: "luxury" in c),     # Chapter 87 only
    }

    if m["code"] is None or m["description"] is None:
        return None

    labelled = {v for v in m.values() if v is not None}

    # Preferential-duty country sub-columns, read from the sub-label row.
    # "SG" also names the PAL / Cess sub-columns further right, so keep only the
    # first (left-most) occurrence of each code — that is the real country block.
    prefs = {}
    for idx, text in enumerate(sub_row):
        code = text.upper()
        if code in PREF_COUNTRIES and code not in prefs:
            prefs[code] = idx
    m["preferential"] = prefs

    # PAL and Cess each carry an unlabelled "SG" sub-column immediately to the
    # right of the labelled "Gen" column (only when that neighbour really is a
    # sub-column and not the next labelled duty).
    def sg_sub(base):
        if base is None:
            return None
        nxt = base + 1
        if nxt in labelled:
            return None
        if nxt < len(sub_row) and sub_row[nxt] in ("sg", "gen"):
            return nxt
        return None

    m["pal_sg"]  = sg_sub(m["pal"])
    m["cess_sg"] = sg_sub(m["cess"])

    # Luxury Tax spans two columns: "Free threshold Value" then
    # "Rate on the amount exceeding …".
    m["luxury_rate"] = m["luxury"] + 1 if m["luxury"] is not None else None

    # The dash-marker column ("-", "--", "---") sits between the code and the
    # description and carries the outline depth of each row.
    m["marker"] = (m["code"] + 1
                   if m["description"] == m["code"] + 2
                   else None)

    return m


# ── Row extraction ──────────────────────────────────────────────────────────
_CODE_RE     = re.compile(r"^(\d{4}\.\d{2}(?:\.\d{2,3})?)\b(.*)$")
_HEADING_RE  = re.compile(r"^\d{1,4}\.\d{2}$")
_DASHES_RE   = re.compile(r"^([-–—]+)")
# A repeating page header sometimes collides with the first data row, leaving a
# cell like "HS Code 8703.23.80" or "Description Motor cars …".
_LABEL_PREFIX_RE = re.compile(r"^(hs\s*code|hs\s*hdg|description|un\s*it)\s+", re.I)
# Generic sub-category labels that add no meaning to the search text.
_GENERIC_LABELS = {"other", "others"}


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return None
    return _clean(row[idx])


def _outline_depth(row, colmap, desc):
    """Outline nesting depth of a row: 1 for '-', 2 for '--', 3 for '---', …

    Reads the dedicated marker column, falling back to leading dashes on the
    description when pdfplumber merges that column away."""
    raw = _cell(row, colmap.get("marker"))
    if raw and set(raw) <= {"-", "–", "—"}:
        return len(raw)
    mm = _DASHES_RE.match(desc or "")
    return len(mm.group(1)) if mm else 0


def _rate(row, idx):
    return parse_rate(_cell(row, idx))


def extract_codes_from_pdf(pdf_path):
    """Extract every product-level HS code from one chapter PDF."""
    chapter_number, category = get_chapter_title_from_pdf(pdf_path)
    source_file = os.path.basename(pdf_path)

    records = []
    colmap = None
    current_heading = None
    current_heading_desc = None
    # Stack of (depth, label) for the "- Sheep :" / "- Liquefied :" style
    # sub-category rows that sit between a heading and its product codes.
    subcats = []

    def ancestor_path(depth):
        return [label for d, label in subcats
                if d < depth and label.lower() not in _GENERIC_LABELS]

    def push_subcat(depth, label):
        subcats[:] = [s for s in subcats if s[0] < depth]
        if label:
            subcats.append((depth, label))

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables(TABLE_SETTINGS) or []:
                header_map = build_column_map(table)
                if header_map:
                    colmap = header_map
                if colmap is None:
                    continue

                for row in table:
                    # Skip the header rows themselves — including the rare case
                    # where a page header collides with the first data row and
                    # its rate cells are actually column labels.
                    if any("hs code" in _norm(c) for c in row):
                        continue

                    heading_raw = _cell(row, colmap["heading"])
                    code_raw    = _cell(row, colmap["code"])
                    desc_raw    = _cell(row, colmap["description"])

                    if code_raw:
                        code_raw = _LABEL_PREFIX_RE.sub("", code_raw)
                    if desc_raw:
                        desc_raw = _LABEL_PREFIX_RE.sub("", desc_raw)

                    # A code cell can arrive merged with dashes / description,
                    # e.g. "8703.33.79 ---- Other" — split the real code off.
                    code = None
                    if code_raw:
                        mm = _CODE_RE.match(code_raw)
                        if mm:
                            code = mm.group(1)
                            trailing = mm.group(2).strip(" -–—:")
                            if not desc_raw and trailing:
                                desc_raw = _clean(trailing)

                    # Heading row: a heading number, no product code.
                    if not code and heading_raw and _HEADING_RE.match(heading_raw):
                        current_heading = heading_raw
                        current_heading_desc = desc_raw
                        subcats.clear()
                        continue

                    if not code:
                        # Sub-category label row ("- Sheep :", "- Liquefied :").
                        # Not a product itself, but context for the codes below.
                        if desc_raw:
                            depth = _outline_depth(row, colmap, desc_raw)
                            if depth > 0:
                                label = _DASHES_RE.sub("", desc_raw).strip(" :–—-").strip()
                                push_subcat(depth, label)
                        continue

                    # Outline context: the "- Sheep :" / "- Liquefied :" labels
                    # that this code sits under. A code row whose description
                    # ends with ":" (e.g. "8415.90.10 --- Outdoor units … :") is
                    # itself a grouping label for the codes that follow.
                    depth = _outline_depth(row, colmap, desc_raw) or code.count(".") + 1
                    classification_path = ancestor_path(depth)
                    if desc_raw and desc_raw.rstrip().endswith(":"):
                        push_subcat(depth, desc_raw.rstrip(" :–—-").strip())

                    luxury = None
                    if colmap["luxury"] is not None:
                        threshold_raw = _cell(row, colmap["luxury"])
                        luxury = {
                            "freeThreshold":    threshold_raw,
                            "freeThresholdAmount": parse_rate(threshold_raw).get("flatAmount"),
                            "rateOnExcess":     _rate(row, colmap["luxury_rate"]),
                        }

                    preferential = {
                        country: parse_rate(_cell(row, idx))
                        for country, idx in colmap["preferential"].items()
                    }

                    records.append({
                        "code":               code,
                        "description":        desc_raw or "",
                        "heading":            current_heading,
                        "headingDescription": current_heading_desc,
                        "classificationPath": classification_path,
                        "chapter":            chapter_number,
                        "category":           category,
                        "unit":               _cell(row, colmap["unit"]),
                        "iclSlsi":            _cell(row, colmap["icl_slsi"]),
                        # National duties
                        "cid_rate":           _rate(row, colmap["gen_duty"]),
                        "vat_rate":           _rate(row, colmap["vat"]),
                        "pal_gen_rate":       _rate(row, colmap["pal"]),
                        "pal_sg_rate":        _rate(row, colmap["pal_sg"]),
                        "cess_gen_rate":      _rate(row, colmap["cess"]),
                        "cess_sg_rate":       _rate(row, colmap["cess_sg"]),
                        "excise_rate":        _rate(row, colmap["excise"]),
                        "scd_rate":           _rate(row, colmap["scd"]),
                        "sscl_rate":          _rate(row, colmap["sscl"]),
                        "scl_rate":           _rate(row, colmap["scl"]),
                        "luxuryTax":          luxury,
                        # Trade-agreement preferential rates
                        "preferential":       preferential,
                        "compliance":         [],
                        "sourceFile":         source_file,
                    })

    return _drop_parent_headings(records)


def _drop_parent_headings(records):
    """Remove six-digit parents whose only role is to group their eight/ten-digit
    children (e.g. 8501.10 when 8501.10.10 / 8501.10.90 exist), and rows that
    carry no unit and no real rate (mis-captured label rows)."""
    # De-duplicate by code, preferring the row with the most information.
    by_code = {}
    for rec in records:
        prev = by_code.get(rec["code"])
        if prev is None or _info_score(rec) > _info_score(prev):
            by_code[rec["code"]] = rec

    codes = set(by_code)

    def is_parent(code):
        prefix = code + "."
        return any(other != code and other.startswith(prefix) for other in codes)

    kept = []
    for code, rec in by_code.items():
        if is_parent(code):
            continue
        if rec["unit"] is None and not _has_real_rate(rec):
            continue
        kept.append(rec)

    kept.sort(key=lambda r: r["code"])
    return kept


_RATE_FIELDS = (
    "cid_rate", "vat_rate", "pal_gen_rate", "cess_gen_rate",
    "excise_rate", "scd_rate", "sscl_rate", "scl_rate",
)


def _has_real_rate(rec):
    return any(rec[f].get("type") not in ("none", "unknown") for f in _RATE_FIELDS)


def _info_score(rec):
    score = 0
    if rec["unit"]:
        score += 1
    score += sum(1 for f in _RATE_FIELDS if rec[f].get("type") not in ("none", "unknown"))
    score += len(rec["description"]) / 100
    return score


# ── Embedding text ──────────────────────────────────────────────────────────
def build_embedding_text(rec):
    """[sub-category …] | description | headingDescription | category —
    the searchable summary. The sub-category labels ("Sheep", "Liquefied")
    are what separate otherwise-identical siblings such as 0104.10.10 and
    0104.20.10, both "Pure-bred breeding animals"."""
    parts = list(rec.get("classificationPath") or [])
    parts.append(rec["description"])
    if rec.get("headingDescription"):
        parts.append(rec["headingDescription"])
    if rec.get("category"):
        parts.append(rec["category"])
    return " | ".join(p for p in parts if p)


# ── Pinecone ────────────────────────────────────────────────────────────────
def get_pinecone_index():
    from pinecone import Pinecone

    pc = Pinecone(api_key=PINECONE_API_KEY)

    if not pc.has_index(PINECONE_INDEX_NAME):
        print(f"Creating Pinecone index '{PINECONE_INDEX_NAME}' with integrated "
              f"model '{PINECONE_MODEL}' ...")
        pc.create_index_for_model(
            name=PINECONE_INDEX_NAME,
            cloud="aws",
            region="us-east-1",
            embed={
                "model":     PINECONE_MODEL,
                "metric":    "cosine",
                "field_map": {"text": "text"},
            },
        )
        while not pc.describe_index(PINECONE_INDEX_NAME).status["ready"]:
            time.sleep(1)
        print("Index ready.")

    return pc.Index(PINECONE_INDEX_NAME)


def already_in_pinecone(index, codes_batch):
    """Set of HS code strings that already have a record in the index."""
    ids = [item["code"] for item in codes_batch]
    try:
        response = index.fetch(ids=ids, namespace=PINECONE_NAMESPACE)
        return set(response.vectors.keys())
    except Exception:
        return set()


# ── Seed pipeline ───────────────────────────────────────────────────────────
def seed_with_resumability(codes):
    """
    Two-phase pipeline:
      1. Write structured data to Firestore for every code.
      2. Upsert a text record per code to Pinecone, which embeds it server-side
         with the index's integrated model. Codes that already have a record
         are skipped (safe to re-run).
    """
    index = get_pinecone_index()

    already_done_ids = set()
    for i in range(0, len(codes), 50):
        already_done_ids |= already_in_pinecone(index, codes[i : i + 50])
    print(f"{len(already_done_ids)} codes already in Pinecone — will skip them.")

    pending = [c for c in codes if c["code"] not in already_done_ids]
    already_done = len(codes) - len(pending)

    # Phase 1 — Firestore (data only)
    print(f"\nWriting {len(codes)} codes to Firestore ...")
    for i, item in enumerate(codes, 1):
        db.collection("hscodes").document(item["code"]).set(item)
        if i % 200 == 0:
            print(f"  {i}/{len(codes)} written ...")
    print("Firestore write complete.")

    # Phase 2 — upsert text records; Pinecone embeds them with PINECONE_MODEL
    print(f"\nUpserting {len(pending)} text records to Pinecone ...")
    newly_seeded = 0
    for start in range(0, len(pending), UPSERT_BATCH):
        batch = pending[start : start + UPSERT_BATCH]

        try:
            index.upsert_records(namespace=PINECONE_NAMESPACE, records=[
                {
                    "_id":         item["code"],
                    "text":        build_embedding_text(item),
                    "description": item["description"][:1000],
                    "category":    item.get("category") or "",
                    "heading":     item.get("heading") or "",
                    "chapter":     item.get("chapter") or 0,
                    "subCategory": " > ".join(item.get("classificationPath") or []),
                }
                for item in batch
            ])
        except Exception as e:
            print(f"  upsert failed for batch at {start}: {e}")
            continue

        newly_seeded += len(batch)
        print(f"  upserted {newly_seeded}/{len(pending)} records ...")
        time.sleep(0.1)

    print("\nDone.")
    print(f"Newly upserted to Pinecone:   {newly_seeded}")
    print(f"Already present (skipped):     {already_done}")


# ── Dry run ─────────────────────────────────────────────────────────────────
def dry_run(all_codes):
    """Parse only — print coverage stats and a few sample rows, write nothing."""
    print(f"\nTotal product codes extracted: {len(all_codes)}\n")
    fields = [
        ("cid_rate", "CID"), ("vat_rate", "VAT"), ("pal_gen_rate", "PAL"),
        ("cess_gen_rate", "Cess"), ("excise_rate", "Excise"), ("scd_rate", "SCD"),
        ("sscl_rate", "SSCL"), ("scl_rate", "SCL"),
    ]
    for key, name in fields:
        present = sum(1 for c in all_codes if c[key].get("type") not in ("none", "unknown"))
        print(f"  {name:6}: {present:5}/{len(all_codes)} rows have a real rate")
    lux = sum(1 for c in all_codes if c.get("luxuryTax") and c["luxuryTax"]["freeThreshold"])
    print(f"  Luxury: {lux:5} rows have a threshold")
    paths = sum(1 for c in all_codes if c.get("classificationPath"))
    print(f"  Sub-cat:{paths:5} rows carry a sub-category path")

    print("\nSample rows:")
    for c in all_codes[:3] + all_codes[len(all_codes) // 2 : len(all_codes) // 2 + 3]:
        print(f"\n  {c['code']}  {c['description'][:70]}")
        print(f"    chapter={c['chapter']} heading={c['heading']} unit={c['unit']}")
        if c.get("classificationPath"):
            print(f"    path={c['classificationPath']}")
        print(f"    embed: {build_embedding_text(c)[:110]}")
        for key, name in fields:
            r = c[key]
            if r.get("type") not in ("none", "unknown"):
                print(f"    {name:6}: {r}")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv

    pdf_files = sorted(glob.glob(os.path.join(PDF_FOLDER, "*.pdf")))
    print(f"Found {len(pdf_files)} PDF files in '{PDF_FOLDER}'.")

    all_codes = []
    for pdf_path in pdf_files:
        if MAX_TOTAL_CODES is not None and len(all_codes) >= MAX_TOTAL_CODES:
            break
        codes = extract_codes_from_pdf(pdf_path)
        if MAX_TOTAL_CODES is not None:
            codes = codes[: MAX_TOTAL_CODES - len(all_codes)]
        print(f"  {os.path.basename(pdf_path)}: {len(codes)} codes")
        all_codes.extend(codes)

    print(f"\nTotal codes extracted: {len(all_codes)}"
          + (f" (limit: {MAX_TOTAL_CODES})" if MAX_TOTAL_CODES is not None else ""))

    if dry:
        dry_run(all_codes)
    else:
        seed_with_resumability(all_codes)
