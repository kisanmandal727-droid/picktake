import os, uuid, json, sqlite3
import urllib.request
from datetime import datetime
from functools import wraps

import cloudinary
import cloudinary.uploader
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash

# Use PostgreSQL on Render (DATABASE_URL set), SQLite locally
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES  = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

# ── App ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["PERMANENT_SESSION_LIFETIME"] = __import__("datetime").timedelta(days=30)

# ── Cloudinary ─────────────────────────────────────────────────────────────
cloudinary.config(
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "dudaclmew"),
    api_key    = os.environ.get("CLOUDINARY_API_KEY",    "879838742296838"),
    api_secret = os.environ.get("CLOUDINARY_API_SECRET", "V9Es_pvYu_ZrD7FXxWehIjYr924"),
    secure     = True
)

# ── Constants ──────────────────────────────────────────────────────────────
ADMIN_EMAIL  = os.environ.get("ADMIN_EMAIL", "admin@picktake.com")
ALLOWED_IMG  = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_PDF  = {"pdf"}
COLLEGES     = [
    "Christ College, Pune", "SP College", "Fergusson College",
    "Vishwakarma Institute of Information Technology (VIIT)"
]
CATEGORIES   = ["Books", "Notes", "Electronics", "Stationery", "Lab Equipments"]
SLUG_MAP     = {"books": "Books", "notes": "Notes", "electronics": "Electronics",
                "stationery": "Stationery", "lab": "Lab Equipments"}
EMOJI_MAP    = {"Books": "📚", "Notes": "📝", "Electronics": "💻",
                "Stationery": "✏️", "Lab Equipments": "🔬"}

# ── Database connection (Postgres on Render, SQLite locally) ──────────────
class SQLiteCursor:
    """Wraps sqlite3 cursor to behave like psycopg2 RealDictCursor."""
    def __init__(self, conn):
        self._conn = conn
        self._cur  = conn.cursor()

    def execute(self, sql, params=()):
        # Convert %s → ? for SQLite
        sql = sql.replace("%s", "?")
        self._cur.execute(sql, params)

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cur.description]
        return [dict(zip(cols, r)) for r in rows]

    def close(self):
        self._cur.close()


class SQLiteConnWrapper:
    """Wraps sqlite3 connection to provide cursor() and commit()."""
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def cursor(self):
        return SQLiteCursor(self._conn)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._conn.commit()
        self._conn.close()


def get_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor
        )
        return conn
    else:
        return SQLiteConnWrapper("picktake.db")

def init_db():
    serial = "SERIAL" if USE_POSTGRES else "INTEGER"
    now    = "NOW()"  if USE_POSTGRES else "CURRENT_TIMESTAMP"
    tables = [
        f"""CREATE TABLE IF NOT EXISTS users (
                id       {serial} PRIMARY KEY,
                email    TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name     TEXT,
                username TEXT,
                college  TEXT,
                bio      TEXT DEFAULT '',
                avatar   TEXT DEFAULT '',
                is_admin INTEGER DEFAULT 0,
                banned   INTEGER DEFAULT 0
            )""",
        f"""CREATE TABLE IF NOT EXISTS listings (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                price       INTEGER DEFAULT 0,
                username    TEXT NOT NULL,
                image       TEXT DEFAULT '',
                category    TEXT NOT NULL,
                description TEXT DEFAULT '',
                pdf_file    TEXT DEFAULT '',
                sold        INTEGER DEFAULT 0,
                downloads   INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT {now}
            )""",
        """CREATE TABLE IF NOT EXISTS favourites (
                user_email TEXT NOT NULL,
                listing_id TEXT NOT NULL,
                PRIMARY KEY (user_email, listing_id)
            )""",
        f"""CREATE TABLE IF NOT EXISTS messages (
                id         {serial} PRIMARY KEY,
                sender     TEXT NOT NULL,
                recipient  TEXT NOT NULL,
                body       TEXT NOT NULL,
                file_name  TEXT DEFAULT '',
                file_type  TEXT DEFAULT '',
                ts         TEXT NOT NULL,
                is_read    INTEGER DEFAULT 0
            )""",
        """CREATE TABLE IF NOT EXISTS follows (
                follower  TEXT NOT NULL,
                following TEXT NOT NULL,
                PRIMARY KEY (follower, following)
            )""",
        f"""CREATE TABLE IF NOT EXISTS reports (
                id          {serial} PRIMARY KEY,
                reporter    TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id   TEXT NOT NULL,
                target_name TEXT NOT NULL,
                reason      TEXT NOT NULL,
                status      TEXT DEFAULT 'pending',
                ts          TEXT NOT NULL
            )""",
        f"""CREATE TABLE IF NOT EXISTS reviews (
                id         {serial} PRIMARY KEY,
                listing_id TEXT NOT NULL,
                reviewer   TEXT NOT NULL,
                stars      INTEGER NOT NULL,
                message    TEXT DEFAULT '',
                ts         TEXT NOT NULL
            )""",
    ]
    with get_db() as conn:
        cur = conn.cursor()
        for sql in tables:
            cur.execute(sql)
        # Simple migrations
        if USE_POSTGRES:
            # PostgreSQL supports IF NOT EXISTS for ADD COLUMN
            cur.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS downloads INTEGER DEFAULT 0")
        else:
            # SQLite doesn't, but won't abort the transaction on a caught error
            try:
                cur.execute("ALTER TABLE listings ADD COLUMN downloads INTEGER DEFAULT 0")
            except:
                pass # Column likely already exists
        # Seed admin user
        cur.execute("SELECT id FROM users WHERE email=%s", (ADMIN_EMAIL,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (email,password,name,username,college,is_admin) VALUES (%s,%s,%s,%s,%s,1)",
                (ADMIN_EMAIL, generate_password_hash("admin123"), "Admin", "admin", "Pick&Take")
            )
        conn.commit()

# ── Helpers ────────────────────────────────────────────────────────────────
def allowed_file(filename, allowed):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed

UPLOAD_FOLDER = "static/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def save_file(file, allowed):
    """Upload to Cloudinary (production) or local folder (dev fallback)."""
    if not (file and file.filename and allowed_file(file.filename, allowed)):
        return ""
    try:
        ext   = file.filename.rsplit(".", 1)[1].lower()
        rtype = "raw" if ext == "pdf" else "image"
        result = cloudinary.uploader.upload(
            file,
            resource_type = rtype,
            folder        = "picktake",
            public_id     = str(uuid.uuid4())[:12]
        )
        return result["secure_url"]
    except Exception as e:
        print(f"Cloudinary upload error: {e} — saving locally instead")
        # Local fallback for development
        try:
            filename = str(uuid.uuid4())[:12] + "." + ext
            file.seek(0)
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            return "/static/uploads/" + filename
        except Exception as e2:
            print(f"Local save also failed: {e2}")
            return ""

def is_admin():
    return session.get("is_admin", False)

def proxy_download(url, filename, mimetype="application/octet-stream"):
    """Fetch a Cloudinary URL and serve it as a proper download."""
    from flask import Response
    import urllib.request as urlreq
    try:
        req = urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlreq.urlopen(req, timeout=15) as r:
            data = r.read()
        return Response(
            data,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
            }
        )
    except Exception as e:
        print(f"Proxy download error: {e}")
        return redirect(url)

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin():
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated

def img_url(val):
    """Return val as-is (Cloudinary URL) or empty."""
    return val or ""

app.jinja_env.globals["img_url"] = img_url

# ── Auth ───────────────────────────────────────────────────────────────────
@app.route("/")
def root():
    # Show landing page if not logged in, home if logged in
    if "email" in session:
        return redirect(url_for("home"))
    return render_template("landing.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cur.fetchone()
        if user and user["banned"]:
            flash("Your account has been banned.", "error")
        elif user and check_password_hash(user["password"], password):
            session.permanent  = True
            session["email"]    = email
            session["name"]     = user["name"]
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("home"))
        else:
            flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        college  = request.form.get("college", "")
        if not college:
            flash("Please select your college.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        else:
            name = email.split("@")[0]
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO users (email,password,name,username,college) VALUES (%s,%s,%s,%s,%s)",
                        (email, generate_password_hash(password), name, name, college)
                    )
                    conn.commit()
                session.permanent  = True
                session["email"]    = email
                session["name"]     = name
                session["is_admin"] = False
                return redirect(url_for("home"))
            except Exception as e:
                if 'UNIQUE' not in str(e).upper(): raise
                flash("Email already registered.", "error")
    return render_template("signup.html", colleges=COLLEGES)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("root"))

# ── Home ───────────────────────────────────────────────────────────────────
@app.route("/home")
def home():
    q   = request.args.get("q", "").strip()
    cat = request.args.get("category", "").strip()
    # Get logged-in user's college to filter listings (admins see all)
    user_college = None
    if "email" in session and not session.get("is_admin"):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT college FROM users WHERE email=%s", (session["email"],))
            row = cur.fetchone()
            if row:
                user_college = row["college"]
    with get_db() as conn:
        cur = conn.cursor()
        like = "ILIKE" if USE_POSTGRES else "LIKE"
        # Build college filter: join with users table to match seller's college
        college_join  = "JOIN users u ON u.name = listings.username" if user_college else ""
        college_where = "AND u.college=%s" if user_college else ""
        params_college = (user_college,) if user_college else ()
        if q and cat:
            cur.execute(
                f"SELECT listings.* FROM listings {college_join} WHERE (listings.title {like} %s OR listings.username {like} %s) AND listings.category=%s {college_where} ORDER BY listings.created_at DESC",
                (f"%{q}%", f"%{q}%", cat) + params_college)
        elif q:
            cur.execute(
                f"SELECT listings.* FROM listings {college_join} WHERE (listings.title {like} %s OR listings.username {like} %s) {college_where} ORDER BY listings.created_at DESC",
                (f"%{q}%", f"%{q}%") + params_college)
        elif cat:
            cur.execute(
                f"SELECT listings.* FROM listings {college_join} WHERE listings.category=%s {college_where} ORDER BY listings.created_at DESC",
                (cat,) + params_college)
        else:
            cur.execute(
                f"SELECT listings.* FROM listings {college_join} WHERE 1=1 {college_where} ORDER BY listings.created_at DESC",
                params_college)
        rows = [dict(r) for r in cur.fetchall()]
        fav_ids = []
        if "email" in session:
            cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (session["email"],))
            fav_ids = [r["listing_id"] for r in cur.fetchall()]
    return render_template("index.html", listings=rows, query=q,
                           selected_cat=cat, categories=CATEGORIES, fav_ids=fav_ids,
                           user_college=user_college)

# ── Search ─────────────────────────────────────────────────────────────────
@app.route("/search")
def search_results():
    q = request.args.get("q", "").strip()
    if not q:
        return redirect(url_for("home"))
    with get_db() as conn:
        cur = conn.cursor()
        like = "ILIKE" if USE_POSTGRES else "LIKE"
        cur.execute(
            f"SELECT * FROM listings WHERE title {like} %s OR description {like} %s ORDER BY created_at DESC",
            (f"%{q}%", f"%{q}%"))
        items = [dict(r) for r in cur.fetchall()]
        cur.execute(
            f"SELECT * FROM users WHERE (name {like} %s OR username {like} %s OR college {like} %s) AND is_admin=0 AND banned=0",
            (f"%{q}%", f"%{q}%", f"%{q}%"))
        users_found = [dict(r) for r in cur.fetchall()]
        fav_ids = []
        my_following = []
        if "email" in session:
            cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (session["email"],))
            fav_ids = [r["listing_id"] for r in cur.fetchall()]
        if "name" in session:
            cur.execute("SELECT following FROM follows WHERE follower=%s", (session["name"],))
            my_following = [r["following"] for r in cur.fetchall()]
    return render_template("search.html", q=q, items=items, users_found=users_found,
                           fav_ids=fav_ids, my_following=my_following)

# ── Category ───────────────────────────────────────────────────────────────
@app.route("/category/<slug>")
def category(slug):
    display = SLUG_MAP.get(slug.lower(), slug.title())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM listings WHERE category=%s ORDER BY created_at DESC", (display,))
        rows = [dict(r) for r in cur.fetchall()]
        fav_ids = []
        if "email" in session:
            cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (session["email"],))
            fav_ids = [r["listing_id"] for r in cur.fetchall()]
    return render_template("category.html", category_name=display,
                           emoji=EMOJI_MAP.get(display, "📦"),
                           cat_listings=rows, fav_ids=fav_ids)

# ── Listing ────────────────────────────────────────────────────────────────
@app.route("/listing/<listing_id>")
def listing_detail(listing_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM listings WHERE id=%s", (listing_id,))
        _row = cur.fetchone()
        listing = dict(_row) if _row else None
        if not listing:
            flash("Listing not found.", "error")
            return redirect(url_for("home"))
        cur.execute("SELECT * FROM users WHERE name=%s", (listing["username"],))
        _s = cur.fetchone()
        seller = dict(_s) if _s else None
        cur.execute("SELECT * FROM reviews WHERE listing_id=%s ORDER BY id DESC", (listing_id,))
        reviews = [dict(r) for r in cur.fetchall()]
        # average stars
        avg_stars = round(sum(r["stars"] for r in reviews) / len(reviews), 1) if reviews else 0
        # check if current user already reviewed
        user_reviewed = any(r["reviewer"] == session.get("name") for r in reviews)
    is_owner = session.get("name") == listing["username"]
    return render_template("listing.html", listing=listing, seller=seller,
                           is_owner=is_owner, reviews=reviews,
                           avg_stars=avg_stars, user_reviewed=user_reviewed)

@app.route("/listing/<listing_id>/review", methods=["POST"])
def submit_review(listing_id):
    if "email" not in session:
        return redirect(url_for("login"))
    reviewer = session["name"]
    stars   = int(request.form.get("stars", 0))
    message = request.form.get("message", "").strip()
    if not (1 <= stars <= 5):
        flash("Please select a star rating.", "error")
        return redirect(url_for("listing_detail", listing_id=listing_id))
    with get_db() as conn:
        cur = conn.cursor()
        # one review per user per listing
        cur.execute("SELECT id FROM reviews WHERE listing_id=%s AND reviewer=%s", (listing_id, reviewer))
        if cur.fetchone():
            flash("You have already reviewed this listing.", "error")
        else:
            cur.execute(
                "INSERT INTO reviews (listing_id, reviewer, stars, message, ts) VALUES (%s,%s,%s,%s,%s)",
                (listing_id, reviewer, stars, message, datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
            conn.commit()
            flash("Review submitted!", "success")
    return redirect(url_for("listing_detail", listing_id=listing_id))

@app.route("/listing/<listing_id>/review/delete", methods=["POST"])
def delete_review(listing_id):
    if "email" not in session:
        return redirect(url_for("login"))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM reviews WHERE listing_id=%s AND reviewer=%s",
                    (listing_id, session["name"]))
        conn.commit()
    return redirect(url_for("listing_detail", listing_id=listing_id))

@app.route("/listing/<listing_id>/delete", methods=["POST"])
def delete_listing(listing_id):
    if "email" not in session:
        return redirect(url_for("login"))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM listings WHERE id=%s", (listing_id,))
        listing = cur.fetchone()
        if listing and (listing["username"] == session.get("name") or is_admin()):
            cur.execute("DELETE FROM listings WHERE id=%s", (listing_id,))
            cur.execute("DELETE FROM favourites WHERE listing_id=%s", (listing_id,))
            cur.execute("DELETE FROM reviews WHERE listing_id=%s", (listing_id,))
            conn.commit()
            flash("Listing deleted.", "success")
        else:
            flash("Not authorised.", "error")
    return redirect(url_for("home"))

@app.route("/listing/<listing_id>/sold", methods=["POST"])
def mark_sold(listing_id):
    if "email" not in session:
        return redirect(url_for("login"))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM listings WHERE id=%s", (listing_id,))
        listing = cur.fetchone()
        if listing and listing["username"] == session.get("name"):
            cur.execute("UPDATE listings SET sold=%s WHERE id=%s",
                        (0 if listing["sold"] else 1, listing_id))
            conn.commit()
    return redirect(url_for("listing_detail", listing_id=listing_id))

@app.route("/listing/<listing_id>/download")
def download_pdf(listing_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM listings WHERE id=%s", (listing_id,))
        listing = cur.fetchone()
        if listing and listing["pdf_file"]:
            cur.execute("UPDATE listings SET downloads = downloads + 1 WHERE id=%s", (listing_id,))
            conn.commit()
            url      = listing["pdf_file"]
            filename = listing["title"].replace(" ", "_")[:50] + ".pdf"
            return proxy_download(url, filename, "application/pdf")
    flash("No PDF available.", "error")
    return redirect(url_for("listing_detail", listing_id=listing_id))

@app.route("/listing/<listing_id>/report", methods=["POST"])
def report_listing(listing_id):
    if "email" not in session:
        return redirect(url_for("login"))
    reason = request.form.get("reason", "").strip()
    if reason:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title FROM listings WHERE id=%s", (listing_id,))
            row   = cur.fetchone()
            title = row["title"] if row else listing_id
            cur.execute(
                "INSERT INTO reports (reporter,target_type,target_id,target_name,reason,ts) VALUES (%s,%s,%s,%s,%s,%s)",
                (session["name"], "listing", listing_id, title, reason,
                 datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
            conn.commit()
        flash("Report submitted.", "success")
    return redirect(url_for("listing_detail", listing_id=listing_id))

# ── Post listing ───────────────────────────────────────────────────────────
@app.route("/post-listing", methods=["GET", "POST"])
def post_listing():
    if "email" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        title    = request.form.get("title", "").strip()
        category = request.form.get("category", "")
        price    = request.form.get("price", "0")
        desc     = request.form.get("description", "")
        if title and category in CATEGORIES:
            image_url = save_file(request.files.get("image"), ALLOWED_IMG)
            pdf_url   = save_file(request.files.get("pdf"),   ALLOWED_PDF)
            new_id    = str(uuid.uuid4())[:8]
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO listings (id,title,price,username,image,category,description,pdf_file) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (new_id, title, int(price) if price.isdigit() else 0,
                     session.get("name", "Unknown"), image_url, category, desc, pdf_url)
                )
                conn.commit()
            flash("Listing posted successfully!", "success")
            return redirect(url_for("home"))
        flash("Please fill in all required fields.", "error")
    return render_template("post_listing.html", categories=CATEGORIES)

# ── Profile ────────────────────────────────────────────────────────────────
@app.route("/profile")
def profile():
    if "email" not in session:
        return redirect(url_for("login"))
    email = session["email"]
    tab   = request.args.get("tab", "posts")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = dict(cur.fetchone())
        cur.execute("SELECT * FROM listings WHERE username=%s ORDER BY created_at DESC", (user["name"],))
        my_listings = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM listings ORDER BY created_at DESC")
        all_listings = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (email,))
        fav_ids = [r["listing_id"] for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following=%s", (user["name"],))
        follower_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower=%s", (user["name"],))
        following_count = cur.fetchone()["c"]
    # Convert datetime objects for JSON serialization
    for l in all_listings:
        for k, v in l.items():
            if hasattr(v, 'isoformat'):
                l[k] = v.isoformat()
    return render_template("profile.html", user=user, email=email,
                           my_listings=my_listings, tab=tab,
                           fav_ids=fav_ids,
                           follower_count=follower_count,
                           following_count=following_count,
                           listings_json=json.dumps(all_listings))

@app.route("/edit-profile", methods=["GET", "POST"])
def edit_profile():
    if "email" not in session:
        return redirect(url_for("login"))
    email = session["email"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = dict(cur.fetchone())
    if request.method == "POST":
        new_name    = request.form.get("name", "").strip()
        avatar_url  = user["avatar"] or ""
        new_avatar  = save_file(request.files.get("avatar"), ALLOWED_IMG)
        if new_avatar:
            avatar_url = new_avatar
        old_name = user["name"]
        with get_db() as conn:
            cur = conn.cursor()
            # college is permanent - never update it
            cur.execute(
                "UPDATE users SET name=%s,username=%s,bio=%s,avatar=%s WHERE email=%s",
                (new_name, request.form.get("username", "").strip(),
                 request.form.get("bio", "").strip(),
                 avatar_url, email)
            )
            # Update name in all related tables so listings stay linked
            if new_name and new_name != old_name:
                cur.execute("UPDATE listings SET username=%s WHERE username=%s", (new_name, old_name))
                cur.execute("UPDATE messages SET sender=%s WHERE sender=%s",    (new_name, old_name))
                cur.execute("UPDATE messages SET recipient=%s WHERE recipient=%s", (new_name, old_name))
                cur.execute("UPDATE follows SET follower=%s WHERE follower=%s", (new_name, old_name))
                cur.execute("UPDATE follows SET following=%s WHERE following=%s", (new_name, old_name))
                cur.execute("UPDATE reports SET reporter=%s WHERE reporter=%s", (new_name, old_name))
            conn.commit()
        session["name"] = new_name
        flash("Profile updated!", "success")
        return redirect(url_for("profile"))
    return render_template("edit_profile.html", user=user, email=email)

@app.route("/delete-account", methods=["POST"])
def delete_account():
    if "email" not in session:
        return redirect(url_for("login"))
    email = session["email"]
    name  = session.get("name", "")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM favourites WHERE user_email=%s", (email,))
        cur.execute("DELETE FROM messages WHERE sender=%s OR recipient=%s", (name, name))
        cur.execute("DELETE FROM follows WHERE follower=%s OR following=%s", (name, name))
        cur.execute("DELETE FROM reports WHERE reporter=%s", (name,))
        cur.execute("DELETE FROM reviews WHERE reviewer=%s", (name,))
        cur.execute("DELETE FROM listings WHERE username=%s", (name,))
        cur.execute("DELETE FROM users WHERE email=%s", (email,))
        conn.commit()
    session.clear()
    flash("Account deleted successfully.", "success")
    return redirect(url_for("login"))

# ── Public profile ─────────────────────────────────────────────────────────
@app.route("/user/<username>")
def public_profile(username):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE name=%s OR username=%s", (username, username))
        _u = cur.fetchone()
        user = dict(_u) if _u else None
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("home"))
        cur.execute("SELECT * FROM listings WHERE username=%s ORDER BY created_at DESC", (username,))
        user_listings = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following=%s", (username,))
        follower_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower=%s", (username,))
        following_count = cur.fetchone()["c"]
        fav_ids = []
        if "email" in session:
            cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (session["email"],))
            fav_ids = [r["listing_id"] for r in cur.fetchall()]
        is_following = False
        if "name" in session:
            cur.execute("SELECT 1 FROM follows WHERE follower=%s AND following=%s",
                        (session["name"], username))
            is_following = bool(cur.fetchone())
    return render_template("public_profile.html", user=user, user_listings=user_listings,
                           follower_count=follower_count, following_count=following_count,
                           fav_ids=fav_ids, is_following=is_following,
                           is_own=session.get("name") == username)

# ── Chat ───────────────────────────────────────────────────────────────────
@app.route("/chat")
def chat():
    if "email" not in session:
        return redirect(url_for("login"))
    my_name = session["name"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT
                CASE WHEN sender=%s THEN recipient ELSE sender END AS other_user
            FROM messages WHERE sender=%s OR recipient=%s
        """, (my_name, my_name, my_name))
        convos = cur.fetchall()
        cur.execute("""
            SELECT sender, COUNT(*) AS cnt FROM messages
            WHERE recipient=%s AND is_read=0 GROUP BY sender
        """, (my_name,))
        unread_map = {r["sender"]: r["cnt"] for r in cur.fetchall()}
        cur.execute("SELECT name,username FROM users WHERE name!=%s AND banned=0", (my_name,))
        users_list = cur.fetchall()
    return render_template("chat.html", convos=convos, unread_map=unread_map,
                           users_list=users_list, active_user=None, messages_list=[])

@app.route("/chat/<other_user>")
def chat_with(other_user):
    if "email" not in session:
        return redirect(url_for("login"))
    my_name = session["name"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM messages
            WHERE (sender=%s AND recipient=%s) OR (sender=%s AND recipient=%s)
            ORDER BY id ASC
        """, (my_name, other_user, other_user, my_name))
        msgs = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            UPDATE messages SET is_read=1
            WHERE sender=%s AND recipient=%s AND is_read=0
        """, (other_user, my_name))
        conn.commit()
        cur.execute("""
            SELECT DISTINCT
                CASE WHEN sender=%s THEN recipient ELSE sender END AS other_user
            FROM messages WHERE sender=%s OR recipient=%s
        """, (my_name, my_name, my_name))
        convos = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT sender, COUNT(*) AS cnt FROM messages
            WHERE recipient=%s AND is_read=0 GROUP BY sender
        """, (my_name,))
        unread_map = {r["sender"]: r["cnt"] for r in cur.fetchall()}
        cur.execute("SELECT name,username FROM users WHERE name!=%s AND banned=0", (my_name,))
        users_list = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM users WHERE name=%s", (other_user,))
        _oi = cur.fetchone()
        other_info = dict(_oi) if _oi else None
    return render_template("chat.html", convos=convos, unread_map=unread_map,
                           users_list=users_list, active_user=other_user,
                           messages_list=msgs, other_info=other_info, my_name=my_name)

@app.route("/chat/send", methods=["POST"])
def send_message():
    if "email" not in session:
        return jsonify({"error": "not logged in"}), 401
    if request.content_type and "multipart" in request.content_type:
        recipient = request.form.get("recipient", "")
        body      = request.form.get("body", "").strip()
        file      = request.files.get("file")
        file_name = file_type = ""
        if file and file.filename:
            ext = file.filename.rsplit(".", 1)[-1].lower()
            if ext in ALLOWED_IMG:
                file_type = "image"
                file_name = save_file(file, ALLOWED_IMG)
            elif ext in ALLOWED_PDF:
                file_type = "pdf"
                file_name = save_file(file, ALLOWED_PDF)
    else:
        data      = request.json or {}
        recipient = data.get("recipient", "")
        body      = data.get("body", "").strip()
        file_name = file_type = ""
    if not recipient or (not body and not file_name):
        return jsonify({"error": "missing fields"}), 400
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (sender,recipient,body,file_name,file_type,ts) VALUES (%s,%s,%s,%s,%s,%s)",
            (session["name"], recipient, body, file_name, file_type,
             datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        conn.commit()
    return jsonify({"status": "ok", "file_name": file_name, "file_type": file_type})


@app.route("/chat/delete/<other_user>", methods=["POST"])
def delete_chat(other_user):
    if "email" not in session:
        return redirect(url_for("login"))
    my_name = session["name"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM messages
            WHERE (sender=%s AND recipient=%s) OR (sender=%s AND recipient=%s)
        """, (my_name, other_user, other_user, my_name))
        conn.commit()
    return redirect(url_for("chat"))


@app.route("/chat/unsend/<int:message_id>", methods=["POST"])
def unsend_message(message_id):
    if "email" not in session:
        return jsonify({"error": "not logged in"}), 401
    my_name = session["name"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM messages WHERE id=%s", (message_id,))
        msg = cur.fetchone()
        if msg and msg["sender"] == my_name:
            cur.execute("DELETE FROM messages WHERE id=%s", (message_id,))
            conn.commit()
            return jsonify({"status": "ok"})
    return jsonify({"error": "not allowed"}), 403


@app.route("/chat/download")
def chat_download():
    """Proxy-download a file sent in chat (image or PDF) with proper filename."""
    url      = request.args.get("url", "")
    filename = request.args.get("name", "file")
    if not url or not url.startswith("http"):
        return "Invalid URL", 400
    ext = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else ""
    if ext == "pdf":
        mimetype = "application/pdf"
        if not filename.endswith(".pdf"):
            filename += ".pdf"
    elif ext in ("jpg", "jpeg", "png", "gif", "webp"):
        mimetype = f"image/{ext}"
    else:
        mimetype = "application/octet-stream"
        if not filename.endswith(".pdf"):
            filename += ".pdf"
    return proxy_download(url, filename, mimetype)

@app.route("/chat/poll/<other_user>")
def poll_messages(other_user):
    if "email" not in session:
        return jsonify([])
    my_name  = session["name"]
    since_id = request.args.get("since", 0, type=int)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM messages
            WHERE ((sender=%s AND recipient=%s) OR (sender=%s AND recipient=%s))
            AND id > %s ORDER BY id ASC
        """, (my_name, other_user, other_user, my_name, since_id))
        msgs = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "UPDATE messages SET is_read=1 WHERE sender=%s AND recipient=%s AND is_read=0",
            (other_user, my_name)
        )
        conn.commit()
    return jsonify([dict(m) for m in msgs])

# ── Follow ─────────────────────────────────────────────────────────────────
@app.route("/follow/<username>", methods=["POST"])
def follow_user(username):
    if "email" not in session:
        return jsonify({"error": "not logged in"}), 401
    me = session["name"]
    if me == username:
        return jsonify({"error": "cannot follow yourself"}), 400
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM follows WHERE follower=%s AND following=%s", (me, username))
        if cur.fetchone():
            cur.execute("DELETE FROM follows WHERE follower=%s AND following=%s", (me, username))
            action = "unfollowed"
        else:
            cur.execute("INSERT INTO follows (follower, following) VALUES (%s,%s)", (me, username))
            action = "followed"
        conn.commit()
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following=%s", (username,))
        count = cur.fetchone()["c"]
    return jsonify({"action": action, "followers": count})

@app.route("/follow-count/<username>")
def follow_count(username):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following=%s", (username,))
        count = cur.fetchone()["c"]
    return jsonify({"count": count})

@app.route("/api/my-follow-counts")
def my_follow_counts():
    if "email" not in session:
        return jsonify({"followers": 0, "following": 0})
    name = session.get("name", "")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following=%s", (name,))
        followers = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower=%s", (name,))
        following = cur.fetchone()["c"]
    return jsonify({"followers": followers, "following": following})

@app.route("/api/followers/<username>")
def get_followers(username):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.name, u.username, u.avatar FROM users u
            JOIN follows f ON f.follower = u.name
            WHERE f.following=%s
        """, (username,))
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify(rows)

@app.route("/api/following/<username>")
def get_following(username):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.name, u.username, u.avatar FROM users u
            JOIN follows f ON f.following = u.name
            WHERE f.follower=%s
        """, (username,))
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify(rows)

# ── Favourites ─────────────────────────────────────────────────────────────
@app.route("/api/favourites")
def get_favourites():
    if "email" not in session:
        return jsonify([])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (session["email"],))
        ids = [r["listing_id"] for r in cur.fetchall()]
    return jsonify(ids)

@app.route("/api/favourites/toggle", methods=["POST"])
def toggle_favourite():
    if "email" not in session:
        return jsonify({"error": "not logged in"}), 401
    lid   = (request.json or {}).get("id")
    email = session["email"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM favourites WHERE user_email=%s AND listing_id=%s", (email, lid))
        if cur.fetchone():
            cur.execute("DELETE FROM favourites WHERE user_email=%s AND listing_id=%s", (email, lid))
            action = "removed"
        else:
            cur.execute("INSERT INTO favourites (user_email, listing_id) VALUES (%s,%s)", (email, lid))
            action = "added"
        conn.commit()
        cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (email,))
        favs = [r["listing_id"] for r in cur.fetchall()]
        # keep favs inside with block so cursor is still valid
    return jsonify({"action": action, "favourites": favs})

# ── Admin ──────────────────────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    tab = request.args.get("tab", "users")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users ORDER BY id")
        users_list = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM listings ORDER BY created_at DESC")
        listings_list = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM reports ORDER BY id DESC")
        reports_list = [dict(r) for r in cur.fetchall()]
    return render_template("admin.html", tab=tab, users_list=users_list,
                           listings_list=listings_list, reports_list=reports_list)

@app.route("/admin/ban/<int:user_id>", methods=["POST"])
@admin_required
def admin_ban(user_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        user = cur.fetchone()
        if user:
            cur.execute("UPDATE users SET banned=%s WHERE id=%s",
                        (0 if user["banned"] else 1, user_id))
            conn.commit()
    return redirect(url_for("admin", tab="users"))

@app.route("/admin/delete-listing/<listing_id>", methods=["POST"])
@admin_required
def admin_delete_listing(listing_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM listings WHERE id=%s", (listing_id,))
        cur.execute("DELETE FROM favourites WHERE listing_id=%s", (listing_id,))
        cur.execute("DELETE FROM reviews WHERE listing_id=%s", (listing_id,))
        conn.commit()
    flash("Listing deleted.", "success")
    return redirect(url_for("admin", tab="listings"))

@app.route("/admin/resolve-report/<int:report_id>", methods=["POST"])
@admin_required
def admin_resolve_report(report_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE reports SET status='resolved' WHERE id=%s", (report_id,))
        conn.commit()
    return redirect(url_for("admin", tab="reports"))

@app.route("/admin/dismiss-report/<int:report_id>", methods=["POST"])
@admin_required
def admin_dismiss_report(report_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE reports SET status='dismissed' WHERE id=%s", (report_id,))
        conn.commit()
    return redirect(url_for("admin", tab="reports"))

# ── Run ────────────────────────────────────────────────────────────────────
# Always init DB on startup (works for both gunicorn and python app.py)
init_db()

if __name__ == "__main__":
    app.run(debug=False)
