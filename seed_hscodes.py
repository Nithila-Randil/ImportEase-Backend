"""
One-off script to seed sample HS code data into Firestore.
Run this once with: python seed_hscodes.py
Not part of the running app — just a setup utility.
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from dotenv import load_dotenv
import os

from firebase_setup import db

from google import genai

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)



sample_codes = [
    {
        "code": "84713000",
        "description": "Portable automatic data processing machines (laptops)",
        "category": "Electronics",
        "unit": "unit",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": [
            {"id": "c1", "requirement": "Import license", "mandatory": True, "description": "Standard import license from Department of Import/Export Control"},
            {"id": "c2", "requirement": "TRC Approval", "mandatory": False, "description": "Required if the device has wireless/radio functionality"}
        ]
    },
    {
        "code": "85171200",
        "description": "Mobile phones and smartphones",
        "category": "Electronics",
        "unit": "unit",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": [
            {"id": "c1", "requirement": "TRC Approval", "mandatory": True, "description": "Telecommunications Regulatory Commission approval required"}
        ]
    },
    {
        "code": "61091000",
        "description": "Cotton T-shirts, knitted",
        "category": "Textiles",
        "unit": "kg",
        "cid_rate": 0.10, "vat_rate": 0.18, "pal_rate": 0.05,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": [
            {"id": "c1", "requirement": "Certificate of Origin", "mandatory": True, "description": "Required for preferential tariff treatment"}
        ]
    },
    {
        "code": "62034200",
        "description": "Men's trousers, cotton",
        "category": "Textiles",
        "unit": "kg",
        "cid_rate": 0.10, "vat_rate": 0.18, "pal_rate": 0.05,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": []
    },
    {
        "code": "87032300",
        "description": "Motor cars, 1500cc to 3000cc",
        "category": "Vehicles",
        "unit": "unit",
        "cid_rate": 0.30, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.05, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": [
            {"id": "c1", "requirement": "Import permit", "mandatory": True, "description": "Vehicle import permit from Department of Motor Traffic"},
            {"id": "c2", "requirement": "Emission compliance certificate", "mandatory": True, "description": "Euro 4 or higher emission standard certificate"}
        ]
    },
    {
        "code": "09012100",
        "description": "Roasted coffee",
        "category": "Food",
        "unit": "kg",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.05,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": [
            {"id": "c1", "requirement": "Food import license", "mandatory": True, "description": "Required from the Food Control Administration Unit"}
        ]
    },
    {
        "code": "17019900",
        "description": "Refined sugar",
        "category": "Food",
        "unit": "kg",
        "cid_rate": 0.00, "vat_rate": 0.18, "pal_rate": 0.05,
        "cess_rate": 0.00, "scl_rate": 0.35, "sscl_rate": 0.025,
        "compliance": [
            {"id": "c1", "requirement": "Import license", "mandatory": True, "description": "Special Commodity Levy applies to sugar imports"}
        ]
    },
    {
        "code": "84433200",
        "description": "Printers, office use",
        "category": "Electronics",
        "unit": "unit",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": []
    },
    {
        "code": "30049000",
        "description": "Medicaments, packaged for retail",
        "category": "Pharmaceuticals",
        "unit": "kg",
        "cid_rate": 0.00, "vat_rate": 0.00, "pal_rate": 0.00,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.00,
        "compliance": [
            {"id": "c1", "requirement": "NMRA registration", "mandatory": True, "description": "National Medicines Regulatory Authority approval required"}
        ]
    },
    {
        "code": "94036000",
        "description": "Wooden furniture",
        "category": "Furniture",
        "unit": "unit",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.10, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": []
    },
    {
        "code": "64029900",
        "description": "Footwear, rubber or plastic outer sole",
        "category": "Footwear",
        "unit": "pair",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": []
    },
    {
        "code": "39269099",
        "description": "Plastic household articles",
        "category": "Plastics",
        "unit": "kg",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.10, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": []
    },
    {
        "code": "82119200",
        "description": "Knives with fixed blades (kitchen/household)",
        "category": "Hardware",
        "unit": "unit",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": []
    },
    {
        "code": "44111400",
        "description": "Fibreboard, medium density (MDF)",
        "category": "Building Materials",
        "unit": "sqm",
        "cid_rate": 0.10, "vat_rate": 0.18, "pal_rate": 0.05,
        "cess_rate": 0.00, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": []
    },
    {
        "code": "33049900",
        "description": "Cosmetics and skincare preparations",
        "category": "Cosmetics",
        "unit": "kg",
        "cid_rate": 0.15, "vat_rate": 0.18, "pal_rate": 0.10,
        "cess_rate": 0.15, "scl_rate": 0.00, "sscl_rate": 0.025,
        "compliance": [
            {"id": "c1", "requirement": "Cosmetics registration", "mandatory": True, "description": "Registration with the Cosmetics, Devices and Drugs Regulatory Authority"}
        ]
    }
]


def seed():
    for item in sample_codes:
        code = item["code"]

        result = client.models.embed_content(
            model="gemini-embedding-001",
            contents=item["description"]
        )
        item["embedding"] = result.embeddings[0].values

        db.collection("hscodes").document(code).set(item)
        print(f"Seeded {code} — {item['description']}")

    print(f"\nDone. {len(sample_codes)} HS codes seeded.")

if __name__ == "__main__":
    seed()
