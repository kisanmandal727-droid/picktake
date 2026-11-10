# Deploy Pick&Take on Render

## Before deploying

1. Rotate the Cloudinary API key and secret that were previously stored in this project. If you deployed an earlier version, also replace `SECRET_KEY` with a new random value; Render preserves existing values when syncing a Blueprint. Removing old values from files does not invalidate them. If the project was pushed to GitHub, rotate the credentials and remove the old values from repository history.
2. Keep `picktake.db`, `.env`, and `static/uploads/` out of Git. The included SQLite file is for local development; Render uses PostgreSQL.
3. Use a paid Render PostgreSQL database for a production marketplace. Render's Free Postgres databases expire after 30 days and do not include backups.

## Create the Render service

1. Push the project to a private GitHub repository. Check that `picktake.db` and any real `.env` file are not included.
2. In Render, create a Blueprint from the repository. Render generates `SECRET_KEY` and connects the PostgreSQL database from `render.yaml`.
3. Provide fresh Cloudinary values, an administrator email, and an administrator password when prompted. If this Blueprint already exists, add the `sync: false` variables manually in the service's Environment settings.
4. Choose the appropriate PostgreSQL plan before accepting real users. The Free plan is only suitable for a short-lived demonstration.

## Initialize the database and admin

After the first deploy succeeds, open the web service Shell and run:

```text
python startup.py
```

For an existing database, create an export or backup before running the migration. This creates or migrates the tables, safely attaches legacy records where their owner is unambiguous, disables the previously documented default admin account, and provisions the administrator using `ADMIN_EMAIL` and `ADMIN_PASSWORD`. The administrator password must be at least 14 characters. Legacy records that cannot be tied to one account remain unassigned until manually reconciled. Initialization stops if it finds case-insensitive duplicate email addresses; resolve those before rerunning. After setup, remove `ADMIN_PASSWORD` from the Render service environment. Run `python startup.py` again after future schema updates.

There is no default admin password. Do not use `admin@picktake.com` for the new administrator account.

## Verify the service

Render checks `/healthz` after deployment. Once the database has been initialized, open the service URL, create a regular account, and confirm sign-in, listing creation, chat, and file upload. Uploads are limited to 10 MB and stored in Cloudinary in production.

## Local development

Copy `.env.example` to `.env`, install `requirements.txt`, and set a random local `SECRET_KEY`. Local SQLite mode can save uploads under `static/uploads/` if Cloudinary is not configured. Keep the database and uploads local; they are excluded from Git.
