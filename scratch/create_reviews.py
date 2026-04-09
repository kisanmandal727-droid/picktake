import sqlite3
conn = sqlite3.connect('picktake.db')
cur = conn.cursor()
cur.execute("""
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        listing_id TEXT NOT NULL,
        reviewer TEXT NOT NULL,
        stars INTEGER NOT NULL,
        message TEXT DEFAULT '',
        ts TEXT NOT NULL
    )
""")
conn.commit()
conn.close()
print("Reviews table created successfully.")
