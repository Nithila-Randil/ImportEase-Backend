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


def _rate_display(rate_obj):
    """The tariff's own printed rate, e.g. "15%", "Free", "LKR 5/kg" -- for
    showing next to a calculated amount so the user sees what rate was used,
    not just the resulting LKR figure."""
    if not rate_obj or not isinstance(rate_obj, dict):
        return None
    return rate_obj.get("raw")


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


def _gen_or_sg_amount(rate_general, rate_sg, cif, origin_is_singapore):
    """PAL and Cess each carry two national-duty columns in the tariff: a
    general rate and an "SG" rate. Verified directly from the source PDFs'
    own two-row header (both chapters checked): "SG" here is the same code
    used for the Sri Lanka-Singapore FTA in the Preferential Duty block --
    it is NOT a SAFTA/SAARC rate, despite that being a common assumption.
    Use it only for a genuine Singapore origin, and only when the cell has a
    real value (a blank means "no separate SG rate", not "free").

    Returns (amount, rate_display)."""
    if origin_is_singapore and rate_sg and rate_sg.get("raw") and rate_sg.get("type") != "unknown":
        return calculate_rate_amount(rate_sg, cif), _rate_display(rate_sg)
    return calculate_rate_amount(rate_general, cif), _rate_display(rate_general)


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
    "AP": {"name": "Asia-Pacific Trade Agreement (APTA)", "short": "APTA",
           "countries": {"bangladesh", "china", "india", "south korea", "laos"}},
    "AD": {"name": "APTA — Least Developed Countries", "short": "APTA",
           "countries": {"bangladesh", "laos"}},
    "BN": {"name": "Imports from Bangladesh", "short": "APTA",
           "countries": {"bangladesh"}},
    "GT": {"name": "Global System of Trade Preferences (GSTP)", "short": "GSTP",
           "countries": _GSTP_COUNTRIES},
    "IN": {"name": "Indo-Sri Lanka FTA (ISFTA)", "short": "ISFTA",
           "countries": {"india"}},
    "PK": {"name": "Pakistan-Sri Lanka FTA (PSFTA)", "short": "PSFTA",
           "countries": {"pakistan"}},
    "SA": {"name": "SAARC countries (SAPTA)", "short": "SAPTA",
           "countries": _SAARC_COUNTRIES},
    "SF": {"name": "South Asian Free Trade Area (SAFTA)", "short": "SAFTA",
           "countries": _SAARC_COUNTRIES},
    "SD": {"name": "SAFTA — Least Developed Countries", "short": "SAFTA",
           "countries": {"bangladesh", "bhutan", "maldives", "nepal"}},
    "SG": {"name": "Sri Lanka-Singapore FTA (SLSFTA)", "short": "SLSFTA",
           "countries": {"singapore"}},
}

# When a country qualifies under more than one agreement (e.g. India is in
# both APTA and ISFTA), this is the order used to pick which one labels it in
# the origin dropdown -- bilateral deals first, broad multilateral ones last.
# This is purely a display label; _resolve_cid() below still checks every
# agreement a country belongs to and always uses whichever rate is cheapest,
# regardless of this label.
_COUNTRY_LABEL_PRIORITY = ["IN", "PK", "SG", "BN", "AP", "SF", "SA", "AD", "SD", "GT"]


def list_preferential_countries():
    """One entry per country covered by any trade agreement, each with a
    single display label, e.g. {"country": "India", "label": "India (ISFTA)"}.
    Used to populate the origin-country dropdown."""
    country_to_code = {}
    for code in _COUNTRY_LABEL_PRIORITY:
        for country in PREFERENTIAL_AGREEMENTS[code]["countries"]:
            country_to_code.setdefault(country, code)

    return sorted(
        (
            {
                "country": country.title(),
                "label": f"{country.title()} ({PREFERENTIAL_AGREEMENTS[code]['short']})",
            }
            for country, code in country_to_code.items()
        ),
        key=lambda c: c["country"],
    )


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
    """Returns (cid_amount, basis, rate_display) for the Customs Import Duty.

    Starts from the general (Gen Duty) rate. If an origin country is given and it
    qualifies for a preferential agreement that has a real rate cell for this
    code (not a blank), and that rate works out cheaper, use it instead.

    basis is "general" or e.g. "preferential:IN (Indo-Sri Lanka FTA (ISFTA))".
    rate_display is the tariff's own printed rate for whichever one won, e.g.
    "15%" or "Free" -- straight from the "raw" field, for showing alongside
    the calculated amount.
    """
    cid_rate = data.get("cid_rate")
    general = calculate_rate_amount(cid_rate, cif)
    if not origin:
        return general, "general", _rate_display(cid_rate)

    country = _normalize_country(origin)
    preferential = data.get("preferential") or {}

    best_amount = general
    best_basis = "general"
    best_display = _rate_display(cid_rate)
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
            best_display = _rate_display(rate)

    return best_amount, best_basis, best_display


def calculate_landed_cost(data, cif, origin=None):
    """Given an HS code's stored duty data and a declared CIF value, returns the
    estimated landed-cost breakdown.

    `origin` is an optional country of origin. When it qualifies for a trade
    agreement that gives this code a lower Customs Import Duty, that preferential
    rate is used instead of the general rate and `cidBasis` records which.

    Layer order -- matches the seven-layer model importers are generally given
    (CID -> PAL -> Cess -> Excise -> SCL -> SSCL -> VAT), plus two extra real
    tariff columns this data actually has that aren't part of that simplified
    explanation:

        CID    (Gen Duty / preferential)          on CIF
               -- replaced by SCL below for designated goods (fuel, rice,
                  wheat, sugar, etc.), NOT by the whole stack
        SCD    (Surcharge on Customs Duty)        on CID            -- extra column, not in the 7-layer model
        PAL    (Ports & Airports Levy)             on CIF, Gen or SG rate*
        Cess                                        on CIF, Gen or SG rate*
        Excise (Special Provisions Duty)           on CIF + CID + SCD + PAL + Cess
        SCL    (Special Commodity Levy)             on CIF -- REPLACES CID ONLY; every other layer still applies
        SSCL   (Social Security Contribution Levy)  on CIF + CID + SCD + PAL + Cess + Excise + SCL
        VAT                                          on CIF + CID + SCD + PAL + Cess + Excise + SCL + SSCL (everything above, cumulative)
        Luxury Tax (Ch. 87 only)                    on CIF above a published free threshold -- extra, added on top, unaffected by the rest of the stack

        * "SG" here is the Sri Lanka-Singapore FTA rate -- verified from the
          source PDFs' own two-row column header (same code used for the
          Singapore column in the Preferential Duty block). It is NOT a
          SAFTA/SAARC rate, despite that being a common assumption.

    Every result is flagged `isEstimate: True` and carries `disclaimer` — the
    total is duties + taxes only, with no port/handling/agent/transport charges.
    """
    origin_is_singapore = bool(origin) and _normalize_country(origin) == "singapore"

    cid, cid_basis, cid_rate_display = _resolve_cid(data, cif, origin)
    scd_rate = data.get("scd_rate")
    scd = calculate_rate_amount(scd_rate, cid)  # surcharge ON the customs duty

    pal, pal_rate_display = _gen_or_sg_amount(
        data.get("pal_gen_rate"), data.get("pal_sg_rate"), cif, origin_is_singapore
    )
    cess, cess_rate_display = _gen_or_sg_amount(
        data.get("cess_gen_rate"), data.get("cess_sg_rate"), cif, origin_is_singapore
    )

    excise_rate = data.get("excise_rate")
    excise = calculate_rate_amount(excise_rate, cif + cid + scd + pal + cess)

    # SCL (fixed/flat, or ad valorem for some commodities) replaces CID only
    # for the designated goods that carry it -- every other layer above and
    # below still applies normally.
    scl_rate = data.get("scl_rate")
    scl = calculate_rate_amount(scl_rate, cif)
    if scl_rate and scl_rate.get("type") not in (None, "none"):
        cid = 0.0
        cid_basis = "replaced_by_scl"

    sscl_rate = data.get("sscl_rate")
    sscl_base = cif + cid + scd + pal + cess + excise + scl
    sscl = _percentage_amount(sscl_rate, sscl_base)

    vat_rate = data.get("vat_rate")
    vat_base = sscl_base + sscl
    vat = _percentage_amount(vat_rate, vat_base)

    luxury_obj = data.get("luxuryTax")
    luxury_tax = _luxury_tax_amount(luxury_obj, cif)

    total_landed_cost = cif + cid + scd + pal + cess + excise + scl + sscl + vat + luxury_tax

    return {
        "declaredValue": round(cif, 2),
        "cid": round(cid, 2),
        "cidBasis": cid_basis,
        "scd": round(scd, 2),
        "pal": round(pal, 2),
        "cess": round(cess, 2),
        "excise": round(excise, 2),
        "scl": round(scl, 2),
        "sscl": round(sscl, 2),
        "vat": round(vat, 2),
        "luxuryTax": round(luxury_tax, 2),
        # The tariff's own printed rate for whichever figure was actually
        # used (e.g. "15%", "Free") -- shown next to each amount in the UI.
        "rates": {
            "cid": cid_rate_display,
            "scd": _rate_display(scd_rate),
            "pal": pal_rate_display,
            "cess": cess_rate_display,
            "excise": _rate_display(excise_rate),
            "scl": _rate_display(scl_rate),
            "sscl": _rate_display(sscl_rate),
            "vat": _rate_display(vat_rate),
            "luxuryTax": _rate_display((luxury_obj or {}).get("rateOnExcess")),
        },
        "totalLandedCost": round(total_landed_cost, 2),
        "isEstimate": True,
        "disclaimer": ESTIMATE_DISCLAIMER,
    }