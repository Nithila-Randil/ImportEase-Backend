"""
One-off script to completely clear the 'hscodes' collection in Firestore,
so we can re-seed it fresh from the more accurate PDF-based extraction.

Run with: python wipe_hscodes.py
"""

from firebase_setup import db

def wipe_collection():
    docs = db.collection("hscodes").stream()
    count = 0

    for doc in docs:
        doc.reference.delete()
        count += 1
        if count % 100 == 0:
            print(f"Deleted {count} so far...")

    print(f"\nDone. Deleted {count} documents from 'hscodes'.")


if __name__ == "__main__":
    confirm = input("This will permanently delete ALL documents in 'hscodes'. Type 'yes' to continue: ")
    if confirm.strip().lower() == "yes":
        wipe_collection()
    else:
        print("Cancelled — nothing was deleted.")