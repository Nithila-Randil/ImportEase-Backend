import firebase_admin
from firebase_admin import credentials, firestore


cred = credentials.Certificate("firebase-key.json")
firebase_admin.initialize_app(cred)

# Every Firestore call across the app uses this same client.
# Remember: database_id must be "importease", not the default.
db = firestore.client(database_id="importease")
