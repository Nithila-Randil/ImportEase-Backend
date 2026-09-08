from firebase_setup import db

count_query = db.collection("hscodes").count()
result = count_query.get()

print("Total documents in hscodes:", result[0][0].value)