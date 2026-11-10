"""Initialize the database and optionally create an admin from environment secrets."""
import os
from app import create_admin_account, init_db

init_db()

admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
admin_password = os.environ.get("ADMIN_PASSWORD", "")
if bool(admin_email) != bool(admin_password):
    raise RuntimeError("Set both ADMIN_EMAIL and ADMIN_PASSWORD to bootstrap an administrator.")
if admin_email:
    create_admin_account(admin_email, admin_password)
    print("Database initialized and administrator account provisioned.")
    print("Remove ADMIN_PASSWORD from the service environment after this one-time setup.")
else:
    print("Database initialized. No administrator was created; configure bootstrap credentials and rerun startup.py.")
