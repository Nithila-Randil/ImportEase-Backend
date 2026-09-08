"""
Duty calculation logic — kept separate from hscode_routes.py since this
is complex business logic, distinct from HTTP request/response handling.
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


def calculate_landed_cost(data, cif):
    """Given an HS code's stored duty data and a declared CIF value,
    returns the full landed cost breakdown. Handles the SCL override
    case (replaces the standard duty stack entirely) separately from
    the normal CID/VAT/PAL/Cess/SSCL stack."""
    scl_rate = data.get("scl_rate")
    scl_amount = calculate_rate_amount(scl_rate, cif)

    port_fees = cif * 0.005  # default 0.5% of CIF

    # SCL, when present, REPLACES the normal duty stack entirely
    if scl_rate and scl_rate.get("type") not in (None, "none"):
        total_landed_cost = cif + scl_amount + port_fees
        return {
            "declaredValue": round(cif, 2),
            "cid": 0,
            "vat": 0,
            "pal": 0,
            "cess": 0,
            "scl": round(scl_amount, 2),
            "sscl": 0,
            "portFees": round(port_fees, 2),
            "totalLandedCost": round(total_landed_cost, 2),
            "note": "SCL applies to this code, replacing the standard duty stack"
        }

    # Normal duty stack — no SCL override
    cid = calculate_rate_amount(data.get("cid_rate"), cif)
    pal = calculate_rate_amount(data.get("pal_rate"), cif)
    cess = calculate_rate_amount(data.get("cess_rate"), cif)
    sscl = calculate_rate_amount(data.get("sscl_rate"), cif)

    # VAT is calculated on CIF + CID + PAL + CESS, per Sri Lanka customs practice
    vat_base = cif + cid + pal + cess
    vat_rate_obj = data.get("vat_rate")
    vat_percentage = (
        vat_rate_obj.get("value", 0.0)
        if vat_rate_obj and vat_rate_obj.get("type") == "percentage"
        else 0.0
    )
    vat = vat_base * vat_percentage

    total_landed_cost = cif + cid + vat + pal + cess + sscl + port_fees

    return {
        "declaredValue": round(cif, 2),
        "cid": round(cid, 2),
        "vat": round(vat, 2),
        "pal": round(pal, 2),
        "cess": round(cess, 2),
        "scl": 0,
        "sscl": round(sscl, 2),
        "portFees": round(port_fees, 2),
        "totalLandedCost": round(total_landed_cost, 2)
    }