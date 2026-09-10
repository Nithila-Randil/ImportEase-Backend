"""
Duty calculation logic — kept separate from hscode_routes.py since this
is complex business logic, distinct from HTTP request/response handling.

See tax_column_reference.txt for what each short field key (cid_rate,
pal_gen_rate, scd_rate, luxuryTax, preferential.*, …) maps to in the tariff.
"""


def calculate_rate_amount(rate_obj, cif):
    """Given a structured rate object and the CIF value, returns the
    actual duty amount. Handles all four rate types our extraction
    produces: none, percentage, flat, and whichever_higher."""
    if not rate_obj or not isinstance(rate_obj, dict):
        return 0.0

    rate_type = rate_obj.get("type", "none")

    if rate_type == "none":
        return 0.0

    if rate_type == "percentage":
        return cif * rate_obj.get("value", 0.0)

    if rate_type == "flat":
        return rate_obj.get("flatAmount", 0.0)

    if rate_type == "whichever_higher":
        percentage_amount = cif * rate_obj.get("percentage", 0.0)
        flat_amount = rate_obj.get("flatAmount", 0.0)
        return max(percentage_amount, flat_amount)

    # "unknown" type, or anything unexpected — safest to contribute 0
    # rather than guess, but this should be flagged for manual review
    return 0.0


def _percentage_amount(rate_obj, base):
    """VAT and SSCL are always ad valorem. If the cell parsed as anything other
    than a percentage (a mis-read flat value, "Ex", blank), contribute 0 rather
    than guess."""
    if rate_obj and rate_obj.get("type") == "percentage":
        return base * rate_obj.get("value", 0.0)
    return 0.0


def _luxury_tax_amount(luxury_obj, cif):
    """Chapter 87 only. Luxury Tax applies to the slice of CIF value ABOVE a
    published free threshold, at the 'rate on the amount exceeding' rate."""
    if not luxury_obj or not isinstance(luxury_obj, dict):
        return 0.0
    threshold = luxury_obj.get("freeThresholdAmount") or 0.0
    taxable_excess = max(0.0, cif - threshold)
    return calculate_rate_amount(luxury_obj.get("rateOnExcess"), taxable_excess)


# Shown to the user with every landed-cost result. The figure covers government
# duties and taxes only — it is not a quote for the full cost of importing.
ESTIMATE_DISCLAIMER = (
    "Estimate only. Calculated from published Sri Lanka Customs tariff rates for "
    "the declared CIF value. Excludes port, terminal handling, clearing-agent, "
    "inland transport and bank charges. Actual duty is assessed by Customs."
)


# ── Preferential (trade-agreement) duty ──────────────────────────────────────
# The extractor stores a `preferential` dict on each HS code with one entry per
# column in the tariff's "Preferential Duty" block. The columns and their
# member countries are from "INDICATORS FOR PREFERENTIAL RATES OF DUTY" in the
# Sri Lanka Customs National Imports Tariff Guide preamble.
#
# When more than one column applies to an origin, the cheapest available rate is
# used. A preferential rate is only ever used when it comes out LOWER than the
# general (Gen Duty) rate, so an incomplete map can only fall back, never
# overcharge.
_GSTP_COUNTRIES = {
    "algeria", "argentina", "bangladesh", "benin", "bolivia", "brazil",
    "cameroon", "chile", "colombia", "cuba", "north korea", "ecuador", "egypt",
    "ghana", "guinea", "guyana", "india", "indonesia", "iran", "iraq",
    "south korea", "libya", "malaysia", "mexico", "morocco", "mozambique",
    "nicaragua", "nigeria", "pakistan", "peru", "philippines", "romania",
    "singapore", "sudan", "tanzania", "thailand", "trinidad and tobago",
    "tunisia", "venezuela", "vietnam", "zimbabwe",
}
_SAARC_COUNTRIES = {
    "afghanistan", "bangladesh", "bhutan", "india", "maldives", "nepal", "pakistan",
}

PREFERENTIAL_AGREEMENTS = {
    "AP": {"name": "Asia-Pacific Trade Agreement (APTA)",
           "countries": {"bangladesh", "china", "india", "south korea", "laos"}},
    "AD": {"name": "APTA — Least Developed Countries",
           "countries": {"bangladesh", "laos"}},
    "BN": {"name": "Imports from Bangladesh",
           "countries": {"bangladesh"}},
    "GT": {"name": "Global System of Trade Preferences (GSTP)",
           "countries": _GSTP_COUNTRIES},
    "IN": {"name": "Indo-Sri Lanka FTA (ISFTA)",
           "countries": {"india"}},
    "PK": {"name": "Pakistan-Sri Lanka FTA (PSFTA)",
           "countries": {"pakistan"}},
    "SA": {"name": "SAARC countries (SAPTA)",
           "countries": _SAARC_COUNTRIES},
    "SF": {"name": "South Asian Free Trade Area (SAFTA)",
           "countries": _SAARC_COUNTRIES},
    "SD": {"name": "SAFTA — Least Developed Countries",
           "countries": {"bangladesh", "bhutan", "maldives", "nepal"}},
    "SG": {"name": "Sri Lanka-Singapore FTA (SLSFTA)",
           "countries": {"singapore"}},
}

# Accept common spellings / short forms for the country of origin.
_COUNTRY_ALIASES = {
    "prc": "china", "people's republic of china": "china", "mainland china": "china",
    "korea": "south korea", "republic of korea": "south korea",
    "korea, republic of": "south korea", "south korea (rok)": "south korea",
    "dprk": "north korea", "democratic people's republic of korea": "north korea",
    "lao": "laos", "lao pdr": "laos", "lao people's democratic republic": "laos",
    "viet nam": "vietnam", "socialist republic of vietnam": "vietnam",
    "iran (islamic republic of)": "iran", "islamic republic of iran": "iran",
    "libyan arab jamahiriya": "libya",
    "united republic of tanzania": "tanzania",
    "bolivia (plurinational state of)": "bolivia",
    "venezuela (bolivarian republic of)": "venezuela",
    "trinidad & tobago": "trinidad and tobago",
}


def _normalize_country(origin):
    key = str(origin).strip().lower()
    key = key.removeprefix("the ").strip()
    return _COUNTRY_ALIASES.get(key, key)


def _resolve_cid(data, cif, origin):
    """Returns (cid_amount, basis) for the Customs Import Duty.

    Starts from the general (Gen Duty) rate. If an origin country is given and it
    qualifies for a preferential agreement that has a real rate cell for this
    code (not a blank), and that rate works out cheaper, use it instead.

    basis is "general" or e.g. "preferential:IN (Indo-Sri Lanka FTA (ISFTA))".
    """
    general = calculate_rate_amount(data.get("cid_rate"), cif)
    if not origin:
        return general, "general"

    country = _normalize_country(origin)
    preferential = data.get("preferential") or {}

    best_amount = general
    best_basis = "general"
    for code, agreement in PREFERENTIAL_AGREEMENTS.items():
        if country not in agreement["countries"]:
            continue
        rate = preferential.get(code)
        # A blank cell (raw None/empty) means "no concession under this
        # agreement", not "free". An "unknown" parse is too risky to trust.
        if not rate or not rate.get("raw") or rate.get("type") == "unknown":
            continue
        amount = calculate_rate_amount(rate, cif)
        if amount < best_amount:
            best_amount = amount
            best_basis = f"preferential:{code} ({agreement['name']})"

    return best_amount, best_basis


def calculate_landed_cost(data, cif, origin=None):
    """Given an HS code's stored duty data and a declared CIF value, returns the
    estimated landed-cost breakdown.

    `origin` is an optional country of origin. When it qualifies for a trade
    agreement that gives this code a lower Customs Import Duty, that preferential
    rate is used instead of the general rate and `cidBasis` records which.

    The normal stack, in the order the levies build on each other:
        CID (Gen Duty / preferential)     on CIF
        SCD (Surcharge on Customs Duty)    on CID          -- assumption, verify
        PAL (Ports & Airports Levy)        on CIF
        Cess                               on CIF
        Excise (Special Provisions Duty)   on CIF + CID     -- assumption, verify
        VAT                                on CIF + CID + SCD + PAL + Cess + Excise
        SSCL (Social Security Levy)         same base as VAT -- simplified
        Luxury Tax (Ch. 87)                on CIF above the free threshold

    SCL (Special Commodity Levy), when it applies to a code, REPLACES the entire
    stack above.

    Every result is flagged `isEstimate: True` and carries `disclaimer` — the
    total is duties + taxes only, with no port/handling/agent/transport charges.
    """
    scl_rate = data.get("scl_rate")
    scl_amount = calculate_rate_amount(scl_rate, cif)

    # SCL, when present, REPLACES the normal duty stack entirely
    if scl_rate and scl_rate.get("type") not in (None, "none"):
        total_landed_cost = cif + scl_amount
        return {
            "declaredValue": round(cif, 2),
            "cid": 0,
            "cidBasis": "general",
            "scd": 0,
            "pal": 0,
            "cess": 0,
            "excise": 0,
            "vat": 0,
            "sscl": 0,
            "luxuryTax": 0,
            "scl": round(scl_amount, 2),
            "totalLandedCost": round(total_landed_cost, 2),
            "isEstimate": True,
            "disclaimer": ESTIMATE_DISCLAIMER,
            "note": "SCL applies to this code, replacing the standard duty stack",
        }

    # Normal duty stack — no SCL override
    cid, cid_basis = _resolve_cid(data, cif, origin)
    scd    = calculate_rate_amount(data.get("scd_rate"), cid)          # surcharge ON the customs duty
    pal    = calculate_rate_amount(data.get("pal_gen_rate"), cif)
    cess   = calculate_rate_amount(data.get("cess_gen_rate"), cif)
    excise = calculate_rate_amount(data.get("excise_rate"), cif + cid)

    # VAT and SSCL are charged on the accumulated value plus every preceding levy
    levy_base = cif + cid + scd + pal + cess + excise
    vat  = _percentage_amount(data.get("vat_rate"), levy_base)
    sscl = _percentage_amount(data.get("sscl_rate"), levy_base)

    luxury_tax = _luxury_tax_amount(data.get("luxuryTax"), cif)

    total_landed_cost = (
        cif + cid + scd + pal + cess + excise + vat + sscl + luxury_tax
    )

    return {
        "declaredValue": round(cif, 2),
        "cid": round(cid, 2),
        "cidBasis": cid_basis,
        "scd": round(scd, 2),
        "pal": round(pal, 2),
        "cess": round(cess, 2),
        "excise": round(excise, 2),
        "vat": round(vat, 2),
        "sscl": round(sscl, 2),
        "luxuryTax": round(luxury_tax, 2),
        "scl": 0,
        "totalLandedCost": round(total_landed_cost, 2),
        "isEstimate": True,
        "disclaimer": ESTIMATE_DISCLAIMER,
    }