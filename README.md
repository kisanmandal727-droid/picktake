# 🛍️ Pick&Take

A web marketplace where people can post items, discover listings, and connect with other users.

## 🌐 Try Pick&Take

[Open the live app](https://picktake.onrender.com)

## ✨ Features

- Create an account and manage a profile
- Browse categories and search listings
- Post listings, mark items as sold, and manage your posts
- Save listings to favourites
- Follow users and view public profiles
- Chat with other users
- Review listings and report concerns
- Admin tools for moderating users, listings, and reports
- Upload listing images and chat files

## 🧰 Built with

- Python and Flask
- SQLite for local development
- PostgreSQL for deployment
- Cloudinary for production media uploads
- Flask-WTF for form protection
- Gunicorn as the production server

## 💻 Run locally

Requirements: Python 3.

```powershell
git clone https://github.com/kisanmandal727-droid/picktake.git
cd picktake
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set a long, random `SECRET_KEY`. Leave `DATABASE_URL` blank to use local SQLite. Cloudinary settings are optional for local development.

Start the app:

```powershell
python app.py
```

Then open [http://127.0.0.1:5000](http://127.0.0.1:5000).

## 🔐 Administrator account

To create an administrator, set `ADMIN_EMAIL` and a unique `ADMIN_PASSWORD` of at least 14 characters, then run:

```powershell
python startup.py
```

After the account is created, remove `ADMIN_PASSWORD` from the environment.

## 🚀 Deployment

Deployment configuration is in `render.yaml`. See [DEPLOY_STEPS.md](DEPLOY_STEPS.md) for environment variables, database initialization, and deployment guidance.

⚠️ The included free Render database is intended for a short demonstration. Free Render Postgres expires after 30 days, so use persistent database storage before relying on it for real marketplace data.

## 🛡️ Security

Keep `.env`, database files, uploads, and secret credentials out of GitHub. Set production secrets in your hosting provider’s environment settings.
