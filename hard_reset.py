import sqlite3
import os

# delete old database completely
if os.path.exists("artguard.db"):
    os.remove("artguard.db")

conn = sqlite3.connect("artguard.db", timeout=10)
cur = conn.cursor()

cur.execute("""
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE,
    phone TEXT,
    password TEXT
)
""")

cur.execute("""
CREATE TABLE images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT,
    filename TEXT,
    visibility TEXT,
    unique_id TEXT,
    image_hash TEXT
)
""")

cur.execute("""
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id INTEGER,
    user TEXT,
    comment TEXT
)
""")

conn.commit()
conn.close()

print("RESET DONE ✔")