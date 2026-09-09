"""
One-off script to completely clear:
  - The 'hscodes' collection in Firestore
  - All vectors in the Pinecone index

Run with: python clear_db.py
"""

import os
from dotenv import load_dotenv
from firebase_setup import db
from pinecone import Pinecone

load_dotenv()
PINECONE_API_KEY    = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "importease-hscodes")


def wipe_firestore():
    """Delete every document in the 'hscodes' Firestore collection."""
    docs  = db.collection("hscodes").stream()
    count = 0

    for doc in docs:
        doc.reference.delete()
        count += 1
        if count % 100 == 0:
            print(f"  Firestore: deleted {count} so far ...")

    print(f"  Firestore done. Deleted {count} documents from 'hscodes'.")


def wipe_pinecone():
    """Delete all vectors from the Pinecone index."""
    pc    = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(PINECONE_INDEX_NAME)

    # delete_all removes every vector in the default namespace
    index.delete(delete_all=True)
    print(f"  Pinecone done. All vectors deleted from '{PINECONE_INDEX_NAME}'.")


if __name__ == "__main__":
    confirm = input(
        "This will permanently delete ALL documents in Firestore 'hscodes' "
        "AND all vectors in Pinecone. Type 'yes' to continue: "
    )
    if confirm.strip().lower() == "yes":
        print("\nClearing Firestore ...")
        wipe_firestore()
        print("\nClearing Pinecone ...")
        wipe_pinecone()
        print("\nAll clear.")
    else:
        print("Cancelled -- nothing was deleted.")