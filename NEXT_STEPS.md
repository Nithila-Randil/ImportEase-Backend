# ImportEase Backend — What to Build Next

Handoff document for continuing backend work. Reviewed and updated against the
codebase on 2026-09-10.

**Status legend:** ✅ done · 🟡 partly done / needs follow-up · ⬜ not started

---

## Snapshot — what exists today

| Area | Status | File |
| --- | --- | --- |
| Auth & profiles | ✅ | `auth_routes.py` |
| Agencies (register, approve agents, profile) | ✅ | `agency_routes.py` |
| HS code search + detail + landed cost + compliance | ✅ | `hscode_routes.py` |
| HS code extraction from tariff PDFs | ✅ | `import_hscodes_from_pdf.py` |
| Pinecone migration | ✅ (different design — see §1) | `hscode_routes.py`, `import_hscodes_from_pdf.py` |
| Shipments (6-stage pipeline) | ✅ | `shipment_routes.py` |
| Tenders & Bids (incl. bidding eligibility gate) | ✅ | `tender_routes.py` |
| Documents (generate / upload / list / delete) | ✅ (local-disk storage caveat) | `document_routes.py` |
| Notifications (list / mark read / `create_notification` helper) | ✅ | `notification_routes.py` |
| Agent ratings (submit + summary) | ✅ | `agent_rating_routes.py` |
| HS code data actually seeded | 🟡 index is empty — full import not yet run | — |
| `requirements.txt` accuracy | ✅ trimmed to what's imported | `requirements.txt` |
| `portFees` in duty calculator | ✅ removed; results now flagged `isEstimate` + `disclaimer` | `duty_calculator.py` |
| Duty calculator using the newly-extracted duties | ✅ SCD, Excise, Luxury Tax now applied | `duty_calculator.py` |
| Preferential duty by country of origin | ✅ all 10 columns mapped from the tariff preamble | `duty_calculator.py` |
| Role-based route protection (`require_role`) | ✅ built + applied to every wholly role-gated route | `dependencies.py` |
| CORS middleware | ✅ `CORSMiddleware`, origins from `CORS_ORIGINS` env | `main.py` |

---

## 1. Pinecone Migration — ✅ done, but not the way this doc originally planned

The migration is complete and the code is in place. The **approach changed** from
what earlier drafts of this document described, so the old instructions here were
misleading — corrected below.

**What was originally planned (now obsolete):** Gemini generates 3072-dim
embeddings with `gemini-embedding-001`; a `pinecone_setup.py` module; index named
`importease-hscodes`; an `"embedded": true` flag on each Firestore doc for
resumability.

**What was actually built:**

- **No Gemini, no OpenAI, no local embedding code.** The Pinecone index carries
  *integrated embedding* — model **`llama-text-embed-v2`** (1024-dim, cosine).
  We upsert plain text; Pinecone embeds it server-side on both write and query.
- **Index name is `hscode-embeddings`** (read from `PINECONE_INDEX_NAME` in
  `.env`; the hard-coded fallback in all three files matches).
- **No `pinecone_setup.py`.** Each of `hscode_routes.py`,
  `import_hscodes_from_pdf.py` and `clear_db.py` builds its own `Pinecone(...)`
  client. Small duplication; fine for now (could be pulled into a shared module
  later, mirroring `firebase_setup.py`).
- **`import_hscodes_from_pdf.py`:** Firestore stores structured data only (no
  embedding field). Pinecone gets one `upsert_records` call per batch of ≤96
  codes: `{_id, text, description, category, heading, subCategory}`.
- **Resumability:** the Pinecone phase checks the index directly
  (`index.fetch(ids=...)`) and skips codes that already have a record. The
  **Firestore phase is deliberately NOT resumable** — it rewrites every code on
  every run. This was a conscious choice: a `list_documents()` existence check
  would spend read quota, and re-writing ~2,300 tiny docs is cheap enough.
- **`search_hscodes()`:** rewritten to `pinecone_index.search(query={"inputs":
  {"text": q}, "top_k": 10})`, then a Firestore `.get()` per returned code. The
  old `cosine_similarity()` helper and the `numpy` import are gone.
- **`build_embedding_text()`** prepends the sub-category path
  (`classificationPath`) so siblings that share a description — e.g. `0104.10.10`
  and `0104.20.10`, both "Pure-bred breeding animals" — embed as distinct text
  ("Sheep | …" vs "Goats | …").

**What still needs doing:**

1. **Run the full seed.** The index was cleared during development and currently
   holds **0 vectors**. With `MAX_TOTAL_CODES = None` in
   `import_hscodes_from_pdf.py`:
   ```
   python clear_db.py --pinecone        # only if there's stale data; index is empty now
   python import_hscodes_from_pdf.py --dry-run   # sanity-check the parse (~2,278 codes)
   python import_hscodes_from_pdf.py             # full import: Firestore + Pinecone
   ```
2. **Fix `requirements.txt`** (see §9).
3. **Test search quality** against real seeded data — only a single-record smoke
   test has been run so far. Confirm relevant results and that a search costs
   ~10 Firestore reads, not hundreds.
4. **Token budget:** `llama-text-embed-v2` on the Pinecone starter tier has a
   monthly token allowance (5M). A full import of ~2,278 short records is well
   under that — not a concern, but worth knowing before large re-imports.

---

## 2. HS Code Data Seeding — 🟡

The extractor in `import_hscodes_from_pdf.py` was substantially rewritten:

- Column mapping now reads each chapter table's own two-row header and locates
  columns by x-coordinate, instead of assuming fixed indices.
- **Every** national duty in the source is captured: CID (Gen Duty), VAT, PAL,
  Cess, Excise, Surcharge on Customs Duty, SSCL, SCL, plus Luxury Tax
  (Chapter 87) and the ten preferential-duty trade-agreement columns.
- Six-digit parent headings that only group their children are dropped, so they
  are not stored as fake products.
- Sub-category context (`- Sheep :`, `- Liquefied :`) is captured as
  `classificationPath` and folded into the embedding text.
- `--dry-run` prints coverage stats and sample rows without writing anything.

**Remaining:**

- Run the full import (see §1) — currently ~8 chapters, ~2,278 product codes.
- Add more chapter PDFs to `tariff_pdfs/` over time and re-run (resumable for
  Pinecone; Firestore is rewritten each run).
- Spot-check extracted rates against the source PDFs, especially
  `whichever_higher` and any `unknown`-type rates.
- The old "~1,000 Gemini requests/day" constraint **no longer applies** — there
  is no Gemini call anywhere in the pipeline.

---

## 3. `portFees` in the Duty Calculator — ✅ removed

`portFees` (the unsourced `cif * 0.005` placeholder) is gone from
`duty_calculator.py` and the `/hscodes/{code}/landed-cost` response. The total is
now CIF + government duties and taxes only.

Every landed-cost response now carries `isEstimate: true` and a `disclaimer`
string spelling out what the figure excludes (port, terminal handling,
clearing-agent, transport and bank charges). The frontend should surface the
disclaimer wherever it shows a landed-cost figure.

---

## 4. Duty Calculator vs. the enriched extraction — ✅

`calculate_landed_cost()` now applies the levies the extractor added:

- **SCD** (Surcharge on Customs Duty) — charged on the CID amount.
- **Excise** (Special Provisions Duty) — charged on `CIF + CID`.
- **Luxury Tax** (Chapter 87) — charged on the CIF value *above* the free
  threshold, via `luxuryTax.freeThresholdAmount` and `luxuryTax.rateOnExcess`.
- **VAT and SSCL** now share one base: `CIF + CID + SCD + PAL + Cess + Excise`
  (previously SSCL was charged on bare CIF).

The response gained `scd`, `excise`, and `luxuryTax` keys.

**The base rules for SCD and Excise are assumptions** (commented as such in the
file) — check them against a real Customs declaration before trusting the totals.

### Preferential duty by origin — ✅

`/hscodes/{code}/landed-cost` uses the `origin` query param.
`calculate_landed_cost(data, cif, origin=...)` calls `_resolve_cid()`, which
checks whether the origin country qualifies for a trade agreement that has a
*lower* rate for this code and, if so, uses it — reporting which in the new
`cidBasis` field (`"general"` or `"preferential:IN (Indo-Sri Lanka FTA …)"`).
When several agreements apply it takes the cheapest. A preferential rate is only
ever used when it comes out cheaper, so it can never overcharge.

`PREFERENTIAL_AGREEMENTS` maps all ten tariff columns — `AP` (APTA), `AD` (APTA
LDCs: Bangladesh, Laos), `BN` (Bangladesh), `GT` (GSTP — full 40-country list),
`IN`, `PK`, `SA` (SAARC), `SF` (SAFTA), `SD` (SAFTA LDCs), `SG` — taken from
"INDICATORS FOR PREFERENTIAL RATES OF DUTY" in the Tariff Guide preamble.
`_COUNTRY_ALIASES` + `_normalize_country()` accept common spellings ("Viet Nam",
"PRC", "Republic of Korea", …).

A blank preferential cell means "no concession under this agreement" (importer
pays the general rate), *not* "free" — `_resolve_cid` distinguishes the two via
the stored `raw` value, so the extractor must keep carrying `raw`.

---

## 5. Tenders & Bids — ✅

`tender_routes.py` implements every endpoint from the spec:
`POST /shipments/{id}/tender`, `GET /tenders?status=`, `GET /tenders/{id}`,
`GET|POST /tenders/{id}/bids`, `GET|PUT|DELETE /tenders/{id}/bids/{bidId}`,
`POST /tenders/{id}/bids/{bidId}/accept`.

**The bidding eligibility gate is implemented** (`_check_agent_can_bid`): role is
`clearing_agent`, `agentStatus == "approved"`, `profileComplete == true`, the
agency's `profileStatus == "active"`, and `isAgencyAdmin == false` — with a
deliberate exemption so independent solo agents (also flagged `isAgencyAdmin`)
can still bid. Returns `403` with a specific reason otherwise.

Minor follow-ups (flagged in the file's docstring):

- `Tender.volume` is always `None` — Shipments has no volume field.
- `Bid.agentRating` is stored as `None`; Agent Ratings now exists, so this could
  be populated from `GET /agents/{agentId}/ratings` at bid-creation or read time.
- `accept_bid` reads all bids on the tender to reject the losers — fine at
  expected volume.

---

## 6. Documents — ✅ (with a storage caveat)

`document_routes.py` implements generate-CUSDEC, generate-invoice, list, upload
(multipart), and delete.

Caveats carried in the file's docstring, still true:

- **No Firebase Storage bucket is configured.** Uploads are written to a local
  `uploaded_documents/` folder and the path stored on the Firestore record. This
  will not survive on an ephemeral cloud disk — wire up real Firebase Storage
  before deploying.
- "Generate CUSDEC / invoice" create a **metadata record only** — no real PDF is
  produced (no PDF library, and the Document schema has no content/URL field).
- `python-multipart` is now in `requirements.txt`. ✅

---

## 7. Notifications — ✅

`notification_routes.py` implements `GET /notifications?unreadOnly=` and
`PUT /notifications/{id}/read`, plus a `create_notification(user_id, shipment_id,
message)` helper. It is already called from `shipment_routes.py` (on stage
advance) and `tender_routes.py` (on bid acceptance).

Possible extension: fire notifications on more events (bid received, tender
closed, document uploaded, rating received).

---

## 8. Shipments — ✅ (was "built separately by a teammate")

`shipment_routes.py` is merged and complete: create / list / get / update /
delete, plus `GET|PUT /shipments/{id}/status`. The 6-stage pipeline
(`draft → assigned → cusdec_lodged → duty_paid → inspection → cargo_released`)
is enforced strictly forward, one step at a time. Only the assigned agent can
advance a stage; the importer is notified on each change.

No stage-change history is logged — only `currentStage`. This blocks
`averageClearanceTimeHours` in Agent Ratings (§10) and any per-stage timing
analytics. Consider adding a `stageHistory` array (stage + timestamp) on the
shipment.

---

## 9. `requirements.txt` — ✅

Trimmed to exactly what's imported:

```
fastapi
uvicorn
firebase-admin
python-dotenv
pydantic
python-multipart
pinecone
pdfplumber
```

Removed `google-genai`, `pandas`, `openpyxl`, `numpy` (leftovers from the removed
Excel importer and the abandoned Gemini approach — not imported anywhere). Added
`pinecone` and `pdfplumber`. `python-multipart` was also missing from the venv
and is now installed.

---

## 10. Agent Ratings — ✅ (with one gap)

`agent_rating_routes.py` implements `GET /agents/{agentId}/ratings` (the spec
endpoint) plus `POST /shipments/{id}/rating` (added because the spec had no way
to *submit* a rating). A rating requires the shipment to be at `cargo_released`,
the caller to be the owning importer, and no existing rating for that shipment.

`averageClearanceTimeHours` in the summary response is `None` — computing it
honestly needs an "assigned at" timestamp, which depends on the shipment
stage-history change in §8.

---

## 11. Role-Based Route Protection — ✅

`dependencies.py` has `require_role(*roles)` — a dependency factory that layers
on `verify_token`, looks the caller's role up in `users`, and raises 403 if it's
not allowed. It returns the decoded token plus a `"profile"` key holding the
caller's user document, so the route can reuse it without a second read.

Applied to every route that is wholly gated to one role:

| Route | Gate |
| --- | --- |
| `POST /shipments` | `require_role("importer")` |
| `PUT /shipments/{id}/status` | `require_role("clearing_agent")` + assigned-agent check inline |
| `POST /shipments/{id}/tender` | `require_role("importer")` + shipment-owner check inline |
| `POST /shipments/{id}/rating` | `require_role("importer")` + shipment-owner check inline |
| `POST /shipments/{id}/documents` (upload) | `require_role("clearing_agent")` + assigned-agent check inline |

`POST /tenders/{id}/bids` keeps its fuller inline gate `_check_agent_can_bid`
(role + approval + profile + agency status), which supersedes a plain role check.
Read/list routes stay on `verify_token` — they're filtered by ownership, not role.

---

## 12. CORS Setup — ✅

`main.py` now installs `CORSMiddleware`. Allowed origins come from the
`CORS_ORIGINS` env var (comma-separated); it defaults to
`http://localhost:5173,http://127.0.0.1:5173` (Vite dev) when unset.
`allow_credentials`, `allow_methods` and `allow_headers` are all permissive for
the listed origins.

For deployment, set `CORS_ORIGINS` in `.env` to the real frontend URL(s), e.g.
`CORS_ORIGINS=https://app.importease.lk`.

---

## Reference Files

- Existing route files (`auth_routes.py`, `agency_routes.py`,
  `shipment_routes.py`, `tender_routes.py`, …) — the established `APIRouter`
  pattern and conventions to follow.
- `importease-openapi-v2.yaml` and `PROJECT_OVERVIEW.md` are referenced by the
  team but are **not currently in this repo** — get them from whoever holds the
  spec before building new endpoints.
