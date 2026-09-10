"""
One-off script to clear the HS code data stores:
  - The 'hscodes' collection in Firestore
  - All vectors in the Pinecone index

By default it clears BOTH. Pass a flag to clear only one:

  python clear_db.py                 # clear Firestore + Pinecone
  python clear_db.py --firestore     # clear Firestore only
  python clear_db.py --pinecone      # clear Pinecone only
  python clear_db.py --pinecone --yes # skip the confirmation prompt
"""

import argparse
import os

from dotenv import load_dotenv

from firebase_setup import db

load_dotenv()
PINECONE_API_KEY    = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "hscode-embeddings")


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
    from pinecone import Pinecone

    pc    = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index(PINECONE_INDEX_NAME)

    # delete_all removes every vector in the namespace; a missing/empty
    # namespace raises NotFoundException, which just means "already clear".
    try:
        index.delete(delete_all=True, namespace="__default__")
        print(f"  Pinecone done. All vectors deleted from '{PINECONE_INDEX_NAME}'.")
    except Exception as e:
        if "not found" in str(e).lower() or "404" in str(e):
            print(f"  Pinecone already empty ('{PINECONE_INDEX_NAME}').")
        else:
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clear the HS code data stores.")
    parser.add_argument("--firestore", action="store_true",
                        help="Clear the Firestore 'hscodes' collection.")
    parser.add_argument("--pinecone", action="store_true",
                        help=f"Clear the Pinecone index '{PINECONE_INDEX_NAME}'.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    args = parser.parse_args()

    # No target flag given -> clear both (the original behaviour).
    clear_firestore = args.firestore or not (args.firestore or args.pinecone)
    clear_pinecone  = args.pinecone  or not (args.firestore or args.pinecone)

    targets = []
    if clear_firestore:
        targets.append("ALL documents in Firestore 'hscodes'")
    if clear_pinecone:
        targets.append(f"all vectors in Pinecone '{PINECONE_INDEX_NAME}'")

    if not args.yes:
        confirm = input(
            "This will permanently delete " + " AND ".join(targets)
            + ".\nType 'yes' to continue: "
        )
        if confirm.strip().lower() != "yes":
            print("Cancelled -- nothing was deleted.")
            raise SystemExit(0)

    if clear_firestore:
        print("\nClearing Firestore ...")
        wipe_firestore()

    if clear_pinecone:
        print("\nClearing Pinecone ...")
        wipe_pinecone()

    print("\nAll clear.")
