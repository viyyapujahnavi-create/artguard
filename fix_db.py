import sqlite3

conn = sqlite3.connect("artguard.db")
cur = conn.cursor()

print("Fixing database...")

# ---- USERS TABLE UPDATES ----
try:
    cur.execute("ALTER TABLE users ADD COLUMN bio TEXT")
    print("Added bio")
except:
    print("bio already exists")

try:
    cur.execute("ALTER TABLE users ADD COLUMN profile_pic TEXT")
    print("Added profile_pic")
except:
    print("profile_pic already exists")

try:
    cur.execute("ALTER TABLE users ADD COLUMN last_login TEXT")
    print("Added last_login")
except:
    print("last_login already exists")

# ---- PROMPT HISTORY TABLE ----
cur.execute("""
CREATE TABLE IF NOT EXISTS prompts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT,
    prompt TEXT
)
""")
print("prompts table ready")

conn.commit()
conn.close()

print("✅ DATABASE UPDATED SUCCESSFULLY")