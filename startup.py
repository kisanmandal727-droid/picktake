"""Run once to initialise DB tables — use on Render Shell after first deploy."""
from app import init_db
init_db()
print("✅ Database tables created successfully.")
