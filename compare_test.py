from sentence_transformers import SentenceTransformer
import numpy as np

model = SentenceTransformer("all-MiniLM-L6-v2")

descriptions = {
    "84713000": "Portable automatic data processing machines (laptops)",
    "85171200": "Mobile phones and smartphones",
    "61091000": "Cotton T-shirts, knitted",
    "62034200": "Men's trousers, cotton",
    "87032300": "Motor cars, 1500cc to 3000cc",
    "09012100": "Roasted coffee",
    "17019900": "Refined sugar",
    "84433200": "Printers, office use",
    "30049000": "Medicaments, packaged for retail",
    "94036000": "Wooden furniture",
    "64029900": "Footwear, rubber or plastic outer sole",
    "39269099": "Plastic household articles",
    "82119200": "Knives with fixed blades (kitchen/household)",
    "44111400": "Fibreboard, medium density (MDF)",
    "33049900": "Cosmetics and skincare preparations",
}

embeddings = {code: model.encode(desc) for code, desc in descriptions.items()}

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

for q in ["laptop", "tv", "shoes", "car"]:
    q_emb = model.encode(q)
    scores = [(cosine_similarity(q_emb, emb), descriptions[code]) for code, emb in embeddings.items()]
    scores.sort(reverse=True)
    print(f"\n=== Query: '{q}' ===")
    for score, desc in scores[:5]:
        print(f"  {score:.3f} — {desc}")