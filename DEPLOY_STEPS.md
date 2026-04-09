# How to Deploy Pick&Take on Render

## Step 1 — Push to GitHub
1. Create a free account at github.com
2. Create a new repository called `picktake`
3. Upload all files from this folder to it
   (or use: git init → git add . → git commit -m "init" → git push)

## Step 2 — Create Render account
Sign up at https://render.com (free)

## Step 3 — Create PostgreSQL Database on Render
1. Click "New" → "PostgreSQL"
2. Name: `picktake-db`
3. Plan: Free
4. Click "Create Database"
5. Copy the **Internal Database URL** — you'll need it

## Step 4 — Create Web Service on Render
1. Click "New" → "Web Service"
2. Connect your GitHub repo
3. Settings:
   - Name: `picktake`
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
4. Add Environment Variables (click "Environment"):
   ```
   DATABASE_URL     = (paste the Internal Database URL from Step 3)
   SECRET_KEY       = (any long random string, e.g. "abc123xyz789randomstring")
   CLOUDINARY_CLOUD_NAME = dudaclmew
   CLOUDINARY_API_KEY    = 879838742296838
   CLOUDINARY_API_SECRET = V9Es_pvYu_ZrD7FXxWehIjYr924
   ADMIN_EMAIL      = admin@picktake.com
   ```
5. Click "Create Web Service"

## Step 5 — Initialise the Database
After deploy succeeds, open the Render Shell (your service → "Shell" tab) and run:
```
python startup.py
```
This creates all tables and the admin user.

## Step 6 — Done!
Your site is live at `https://picktake.onrender.com`

Admin login:
- Email: admin@picktake.com
- Password: admin123
  (Change this in the DB after first login!)

## Notes
- Free Render services spin down after 15 min of inactivity (cold start ~30s)
- PostgreSQL data persists forever on Render free tier
- All images/PDFs go to Cloudinary — never lost on redeploy
- To upgrade: change Plan to "Starter" ($7/mo) for no spin-down
