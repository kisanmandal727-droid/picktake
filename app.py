import os, uuid, sqlite3, re, secrets
from datetime import datetime
from functools import wraps
from urllib.parse import urlsplit

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify, abort, send_file)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

# Use PostgreSQL on Render (DATABASE_URL set), SQLite locally
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_POSTGRES  = bool(DATABASE_URL)
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"
if IS_PRODUCTION and not USE_POSTGRES:
    raise RuntimeError("DATABASE_URL must be set in production; SQLite is for local development only.")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

# ── App ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY")
if IS_PRODUCTION and not secret_key:
    raise RuntimeError("SECRET_KEY must be set in production.")
app.secret_key = secret_key or secrets.token_urlsafe(48)
app.config.update(
    PERMANENT_SESSION_LIFETIME=__import__("datetime").timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)
csrf = CSRFProtect(app)

# ── Cloudinary ─────────────────────────────────────────────────────────────
IMAGE_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
if IS_PRODUCTION and not all((IMAGE_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)):
    raise RuntimeError("CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET are required in production.")
cloudinary.config(
    cloud_name = IMAGE_CLOUD_NAME,
    api_key    = CLOUDINARY_API_KEY,
    api_secret = CLOUDINARY_API_SECRET,
    secure     = True
)

# ── Constants ──────────────────────────────────────────────────────────────
LEGACY_ADMIN_EMAIL = "admin@picktake.com"
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

    @property
    def lastrowid(self):
        return self._cur.lastrowid


class SQLiteConnWrapper:
    """Wraps sqlite3 connection to provide cursor() and commit()."""
    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def cursor(self):
        return SQLiteCursor(self._conn)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._conn.close()


class PostgresConnWrapper:
    """Closes each PostgreSQL connection after the request-scoped context."""
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type:
                self._conn.rollback()
            else:
                self._conn.commit()
        finally:
            self._conn.close()


def get_db():
    if USE_POSTGRES:
        return PostgresConnWrapper(psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor
        ))
    else:
        return SQLiteConnWrapper(os.path.join(app.root_path, "picktake.db"))

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
                owner_email TEXT,
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
                sender_id  INTEGER,
                recipient_id INTEGER,
                sender     TEXT NOT NULL,
                recipient  TEXT NOT NULL,
                body       TEXT NOT NULL,
                file_name  TEXT DEFAULT '',
                file_type  TEXT DEFAULT '',
                ts         TEXT NOT NULL,
                is_read    INTEGER DEFAULT 0
            )""",
        """CREATE TABLE IF NOT EXISTS follows (
                follower_id INTEGER,
                following_id INTEGER,
                follower  TEXT NOT NULL,
                following TEXT NOT NULL,
                PRIMARY KEY (follower, following)
            )""",
        f"""CREATE TABLE IF NOT EXISTS reports (
                id          {serial} PRIMARY KEY,
                reporter_email TEXT,
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
                reviewer_email TEXT,
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
        # Backward-compatible columns for existing SQLite and PostgreSQL databases.
        migrations = (
            ("listings", "owner_email TEXT"),
            ("messages", "sender_id INTEGER"),
            ("messages", "recipient_id INTEGER"),
            ("follows", "follower_id INTEGER"),
            ("follows", "following_id INTEGER"),
            ("reviews", "reviewer_email TEXT"),
            ("reports", "reporter_email TEXT"),
            ("listings", "downloads INTEGER DEFAULT 0"),
        )
        for table, definition in migrations:
            if USE_POSTGRES:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {definition}")
            else:
                try:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

        # Attach legacy name-based records only when a name maps to one account.
        cur.execute("SELECT id,email,name FROM users")
        users = [dict(row) for row in cur.fetchall()]
        users_by_name = {}
        for user in users:
            key = (user.get("name") or "").casefold()
            if key:
                users_by_name.setdefault(key, []).append(user)
        unique_users = {key: matches[0] for key, matches in users_by_name.items() if len(matches) == 1}

        cur.execute("SELECT id,username FROM listings WHERE owner_email IS NULL")
        for row in cur.fetchall():
            owner = unique_users.get((row["username"] or "").casefold())
            if owner:
                cur.execute("UPDATE listings SET owner_email=%s WHERE id=%s", (owner["email"], row["id"]))

        cur.execute("SELECT id,sender,recipient,sender_id,recipient_id FROM messages")
        for row in cur.fetchall():
            sender = unique_users.get((row["sender"] or "").casefold())
            recipient = unique_users.get((row["recipient"] or "").casefold())
            if sender and row["sender_id"] is None:
                cur.execute("UPDATE messages SET sender_id=%s WHERE id=%s", (sender["id"], row["id"]))
            if recipient and row["recipient_id"] is None:
                cur.execute("UPDATE messages SET recipient_id=%s WHERE id=%s", (recipient["id"], row["id"]))

        cur.execute("SELECT id,reviewer,reviewer_email FROM reviews")
        for row in cur.fetchall():
            reviewer = unique_users.get((row["reviewer"] or "").casefold())
            if reviewer and row["reviewer_email"] is None:
                cur.execute("UPDATE reviews SET reviewer_email=%s WHERE id=%s", (reviewer["email"], row["id"]))

        cur.execute("SELECT follower,following,follower_id,following_id FROM follows")
        for row in cur.fetchall():
            follower = unique_users.get((row["follower"] or "").casefold())
            following = unique_users.get((row["following"] or "").casefold())
            if follower and row["follower_id"] is None:
                cur.execute("UPDATE follows SET follower_id=%s WHERE follower=%s AND following=%s",
                            (follower["id"], row["follower"], row["following"]))
            if following and row["following_id"] is None:
                cur.execute("UPDATE follows SET following_id=%s WHERE follower=%s AND following=%s",
                            (following["id"], row["follower"], row["following"]))

        cur.execute("SELECT id,reporter,reporter_email FROM reports")
        for row in cur.fetchall():
            reporter = unique_users.get((row["reporter"] or "").casefold())
            if reporter and row["reporter_email"] is None:
                cur.execute("UPDATE reports SET reporter_email=%s WHERE id=%s", (reporter["email"], row["id"]))

        cur.execute("SELECT follower,following,follower_id,following_id FROM follows WHERE follower_id IS NOT NULL AND following_id IS NOT NULL")
        seen_follow_pairs = set()
        for row in cur.fetchall():
            pair = (row["follower_id"], row["following_id"])
            if pair in seen_follow_pairs:
                cur.execute("DELETE FROM follows WHERE follower=%s AND following=%s",
                            (row["follower"], row["following"]))
            else:
                seen_follow_pairs.add(pair)

        # Disable the old publicly documented bootstrap account.
        cur.execute("UPDATE users SET is_admin=0,banned=1 WHERE LOWER(email)=%s", (LEGACY_ADMIN_EMAIL,))
        cur.execute("SELECT LOWER(email) AS normalized_email,COUNT(*) AS c FROM users GROUP BY LOWER(email) HAVING COUNT(*)>1")
        if cur.fetchone():
            raise RuntimeError("Resolve duplicate case-insensitive user email addresses before initializing this database.")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users(LOWER(email))")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_owner_email ON listings(owner_email)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_participants ON messages(sender_id,recipient_id,id)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_follows_user_ids ON follows(follower_id,following_id)")
        conn.commit()


def create_admin_account(email, password):
    """Create or promote an admin from explicitly supplied bootstrap credentials."""
    email = (email or "").strip().lower()
    if (not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or email == LEGACY_ADMIN_EMAIL
            or len(email) > 254 or not password or len(password) < 14 or len(password) > 128):
        raise ValueError("Set ADMIN_EMAIL and an ADMIN_PASSWORD of at least 14 characters.")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
        row = cur.fetchone()
        password_hash = generate_password_hash(password)
        if row:
            cur.execute("UPDATE users SET password=%s,is_admin=1,banned=0 WHERE id=%s",
                        (password_hash, row["id"]))
        else:
            admin_tag = uuid.uuid4().hex[:8]
            cur.execute(
                "INSERT INTO users (email,password,name,username,college,is_admin,banned) VALUES (%s,%s,%s,%s,%s,1,0)",
                (email, password_hash, "Admin " + admin_tag, "admin-" + admin_tag, "Pick&Take")
            )
        conn.commit()

# ── Helpers ────────────────────────────────────────────────────────────────
def allowed_file(filename, allowed):
    return bool(filename and "." in filename and filename.rsplit(".", 1)[1].lower() in allowed)

UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def file_signature_matches(file, ext):
    file.seek(0)
    header = file.read(16)
    file.seek(0)
    if ext == "pdf":
        return header.startswith(b"%PDF-")
    if ext == "png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return header.startswith(b"\xff\xd8\xff")
    if ext == "gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if ext == "webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False

def safe_upload_url(value, allowed):
    """Accept only this app's local uploads or files in its Cloudinary account."""
    if not value or not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    path = parsed.path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if ext not in allowed:
        return ""
    if not parsed.scheme and not parsed.netloc:
        local_name = rf"/static/uploads/(?:[a-f0-9]{{12}}|[a-f0-9]{{8}}-[a-f0-9]{{3}})\.{re.escape(ext)}"
        if re.fullmatch(local_name, path):
            return path
        return ""
    if (parsed.scheme == "https" and parsed.hostname == "res.cloudinary.com"
            and IMAGE_CLOUD_NAME and path.startswith(f"/{IMAGE_CLOUD_NAME}/")
            and "/picktake/" in path):
        return value
    return ""

def save_file(file, allowed):
    """Upload a validated image or PDF to Cloudinary, or locally in development."""
    if not (file and file.filename and allowed_file(file.filename, allowed)):
        return ""
    ext = file.filename.rsplit(".", 1)[1].lower()
    if not file_signature_matches(file, ext):
        return ""
    try:
        if not IMAGE_CLOUD_NAME or not CLOUDINARY_API_KEY or not CLOUDINARY_API_SECRET:
            raise RuntimeError("Cloudinary is not configured.")
        rtype = "raw" if ext == "pdf" else "image"
        result = cloudinary.uploader.upload(
            file,
            resource_type = rtype,
            folder        = "picktake",
            public_id     = uuid.uuid4().hex[:12]
        )
        url = safe_upload_url(result.get("secure_url", ""), allowed)
        return url
    except Exception as e:
        if IS_PRODUCTION:
            app.logger.error("Cloudinary upload failed (%s).", type(e).__name__)
            return ""
        app.logger.warning("Cloudinary upload failed in development (%s); saving locally.", type(e).__name__)
        try:
            filename = uuid.uuid4().hex[:12] + "." + ext
            file.seek(0)
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            return "/static/uploads/" + filename
        except Exception as e2:
            app.logger.error("Local upload failed (%s).", type(e2).__name__)
            return ""

def is_admin():
    return session.get("is_admin", False)

def authenticated_user():
    if "email" not in session:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email=%s", (session["email"],))
        row = cur.fetchone()
    return dict(row) if row else None

@app.before_request
def refresh_session_from_database():
    if "email" not in session or request.endpoint == "static":
        return None
    user = authenticated_user()
    if not user or user.get("banned"):
        session.clear()
        return None
    session["user_id"] = user["id"]
    session["name"] = user["name"]
    session["is_admin"] = bool(user["is_admin"])
    return None

@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin():
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated

def img_url(val):
    return safe_upload_url(val, ALLOWED_IMG)

app.jinja_env.globals["img_url"] = img_url

# ── Auth ───────────────────────────────────────────────────────────────────
@app.route("/healthz")
def healthz():
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return jsonify({"status": "ok"})
    except Exception as exc:
        app.logger.error("Health check failed (%s).", type(exc).__name__)
        return jsonify({"status": "unavailable"}), 503

@app.route("/")
def root():
    # Show landing page if not logged in, home if logged in
    if "email" in session:
        return redirect(url_for("home"))
    return render_template("landing.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
            user = cur.fetchone()
        if user and user["banned"]:
            flash("Your account has been banned.", "error")
        elif user and len(password) <= 128 and check_password_hash(user["password"], password):
            session.clear()
            session.permanent  = True
            session["email"]    = user["email"]
            session["user_id"]  = user["id"]
            session["name"]     = user["name"]
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("home"))
        else:
            flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        college  = request.form.get("college", "")
        if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            flash("Enter a valid email address.", "error")
        elif len(password) < 12 or len(password) > 128:
            flash("Use a password between 12 and 128 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif college not in COLLEGES:
            flash("Please select a valid college.", "error")
        else:
            name = email.split("@")[0]
            try:
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
                    if cur.fetchone():
                        flash("That email address is already registered.", "error")
                        return render_template("signup.html", colleges=COLLEGES)
                    cur.execute("SELECT id FROM users WHERE LOWER(name)=LOWER(%s) OR LOWER(username)=LOWER(%s)",
                                (name, name))
                    if cur.fetchone():
                        flash("That profile name is already in use. Use a different email address.", "error")
                        return render_template("signup.html", colleges=COLLEGES)
                    cur.execute(
                        "INSERT INTO users (email,password,name,username,college) VALUES (%s,%s,%s,%s,%s)",
                        (email, generate_password_hash(password), name, name, college)
                    )
                    conn.commit()
                    cur.execute("SELECT id FROM users WHERE email=%s", (email,))
                    user_id = cur.fetchone()["id"]
                session.clear()
                session.permanent  = True
                session["email"]    = email
                session["user_id"]  = user_id
                session["name"]     = name
                session["is_admin"] = False
                return redirect(url_for("home"))
            except Exception as e:
                if 'UNIQUE' not in str(e).upper(): raise
                flash("That email address is already registered.", "error")
    return render_template("signup.html", colleges=COLLEGES)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("root"))

# ── Home ───────────────────────────────────────────────────────────────────
@app.route("/home")
def home():
    q   = request.args.get("q", "").strip()[:120]
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
        college_join  = "JOIN users u ON u.email = listings.owner_email" if user_college else ""
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
    q = request.args.get("q", "").strip()[:120]
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
            cur.execute("""SELECT u.name FROM follows f JOIN users u ON u.id=f.following_id
                           WHERE f.follower_id=%s""", (session["user_id"],))
            my_following = [r["name"] for r in cur.fetchall()]
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
        if listing.get("owner_email"):
            cur.execute("SELECT * FROM users WHERE email=%s", (listing["owner_email"],))
            _s = cur.fetchone()
        else:
            cur.execute("SELECT * FROM users WHERE name=%s", (listing["username"],))
            sellers = cur.fetchall()
            _s = sellers[0] if len(sellers) == 1 else None
        seller = dict(_s) if _s else None
        cur.execute("SELECT * FROM reviews WHERE listing_id=%s ORDER BY id DESC", (listing_id,))
        reviews = [dict(r) for r in cur.fetchall()]
        # average stars
        avg_stars = round(sum(r["stars"] for r in reviews) / len(reviews), 1) if reviews else 0
        user_reviewed = any(r.get("reviewer_email") == session.get("email") for r in reviews)
    is_owner = listing.get("owner_email") == session.get("email")
    return render_template("listing.html", listing=listing, seller=seller,
                           is_owner=is_owner, reviews=reviews,
                           avg_stars=avg_stars, user_reviewed=user_reviewed)

@app.route("/listing/<listing_id>/review", methods=["POST"])
def submit_review(listing_id):
    if "email" not in session:
        return redirect(url_for("login"))
    reviewer = session["name"]
    try:
        stars = int(request.form.get("stars", 0))
    except (TypeError, ValueError):
        stars = 0
        message = request.form.get("message", "").strip()
    if not (1 <= stars <= 5) or len(message) > 500:
        flash("Please select a star rating.", "error")
        return redirect(url_for("listing_detail", listing_id=listing_id))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,owner_email FROM listings WHERE id=%s", (listing_id,))
        listing = cur.fetchone()
        if not listing or listing["owner_email"] == session["email"]:
            flash("You cannot review this listing.", "error")
            return redirect(url_for("listing_detail", listing_id=listing_id))
        cur.execute("SELECT id FROM reviews WHERE listing_id=%s AND reviewer_email=%s",
                    (listing_id, session["email"]))
        if cur.fetchone():
            flash("You have already reviewed this listing.", "error")
        else:
            cur.execute(
                "INSERT INTO reviews (listing_id, reviewer_email, reviewer, stars, message, ts) VALUES (%s,%s,%s,%s,%s,%s)",
                (listing_id, session["email"], reviewer, stars, message,
                 datetime.now().strftime("%Y-%m-%d %H:%M"))
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
        cur.execute("DELETE FROM reviews WHERE listing_id=%s AND reviewer_email=%s",
                    (listing_id, session["email"]))
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
        if listing and (listing.get("owner_email") == session.get("email") or is_admin()):
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
        if listing and listing.get("owner_email") == session.get("email"):
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
        safe_url = safe_upload_url(listing["pdf_file"], ALLOWED_PDF) if listing else ""
        if safe_url:
            cur.execute("UPDATE listings SET downloads = downloads + 1 WHERE id=%s", (listing_id,))
            conn.commit()
            return redirect(safe_url)
    flash("No PDF available.", "error")
    return redirect(url_for("listing_detail", listing_id=listing_id))

@app.route("/listing/<listing_id>/report", methods=["POST"])
def report_listing(listing_id):
    if "email" not in session:
        return redirect(url_for("login"))
    reason = request.form.get("reason", "").strip()[:500]
    if reason:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title FROM listings WHERE id=%s", (listing_id,))
            row   = cur.fetchone()
            title = row["title"] if row else listing_id
            cur.execute(
                "INSERT INTO reports (reporter_email,reporter,target_type,target_id,target_name,reason,ts) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (session["email"], session["name"], "listing", listing_id, title, reason,
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
        desc     = request.form.get("description", "").strip()
        if (title and len(title) <= 120 and len(desc) <= 5000
                and category in CATEGORIES and re.fullmatch(r"\d{1,7}", price or "")):
            image_file = request.files.get("image")
            pdf_file = request.files.get("pdf")
            image_url = save_file(image_file, ALLOWED_IMG)
            pdf_url = save_file(pdf_file, ALLOWED_PDF)
            if ((image_file and image_file.filename and not image_url)
                    or (pdf_file and pdf_file.filename and not pdf_url)):
                flash("One of the uploads was invalid or could not be stored. Use a valid image or PDF under 10 MB.", "error")
                return render_template("post_listing.html", categories=CATEGORIES)
            new_id    = str(uuid.uuid4())
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO listings (id,owner_email,title,price,username,image,category,description,pdf_file) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (new_id, session["email"], title, int(price),
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
        cur.execute("SELECT * FROM listings WHERE owner_email=%s ORDER BY created_at DESC", (email,))
        my_listings = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM listings ORDER BY created_at DESC")
        all_listings = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (email,))
        fav_ids = [r["listing_id"] for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following_id=%s", (user["id"],))
        follower_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower_id=%s", (user["id"],))
        following_count = cur.fetchone()["c"]
    # Convert datetime objects for JSON serialization
    for l in all_listings:
        for k, v in l.items():
            if hasattr(v, 'isoformat'):
                l[k] = v.isoformat()
        l["image"] = img_url(l.get("image"))
    return render_template("profile.html", user=user, email=email,
                           my_listings=my_listings, tab=tab,
                           fav_ids=fav_ids,
                           follower_count=follower_count,
                           following_count=following_count,
                           all_listings=all_listings)

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
        new_username = request.form.get("username", "").strip()
        bio = request.form.get("bio", "").strip()
        if (not new_name or not new_username or len(new_name) > 60 or len(new_username) > 40
                or len(bio) > 500):
            flash("Name, username, or bio is invalid or too long.", "error")
            return render_template("edit_profile.html", user=user, email=email)
        avatar_url  = user["avatar"] or ""
        new_avatar  = save_file(request.files.get("avatar"), ALLOWED_IMG)
        if new_avatar:
            avatar_url = new_avatar
        old_name = user["name"]
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT id FROM users WHERE email<>%s AND
                           (LOWER(name) IN (LOWER(%s),LOWER(%s)) OR
                            LOWER(username) IN (LOWER(%s),LOWER(%s)))""",
                        (email, new_name, new_username, new_name, new_username))
            if cur.fetchone():
                flash("That name or username is already in use.", "error")
                return render_template("edit_profile.html", user=user, email=email)
            # college is permanent - never update it
            cur.execute(
                "UPDATE users SET name=%s,username=%s,bio=%s,avatar=%s WHERE email=%s",
                (new_name, new_username, bio, avatar_url, email)
            )
            # Names remain display data; ownership and conversations use stable IDs.
            if new_name and new_name != old_name:
                cur.execute("UPDATE listings SET username=%s WHERE owner_email=%s", (new_name, email))
                cur.execute("UPDATE messages SET sender=%s WHERE sender_id=%s", (new_name, user["id"]))
                cur.execute("UPDATE messages SET recipient=%s WHERE recipient_id=%s", (new_name, user["id"]))
                cur.execute("UPDATE reviews SET reviewer=%s WHERE reviewer_email=%s", (new_name, email))
                cur.execute("UPDATE follows SET follower=%s WHERE follower_id=%s", (new_name, user["id"]))
                cur.execute("UPDATE follows SET following=%s WHERE following_id=%s", (new_name, user["id"]))
                cur.execute("UPDATE reports SET reporter=%s WHERE reporter_email=%s", (new_name, email))
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
    user_id = session.get("user_id")
    name  = session.get("name", "")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM favourites WHERE user_email=%s", (email,))
        cur.execute("DELETE FROM messages WHERE sender_id=%s OR recipient_id=%s", (user_id, user_id))
        cur.execute("DELETE FROM follows WHERE follower_id=%s OR following_id=%s", (user_id, user_id))
        cur.execute("DELETE FROM reports WHERE reporter_email=%s", (email,))
        cur.execute("DELETE FROM reviews WHERE reviewer_email=%s", (email,))
        cur.execute("DELETE FROM listings WHERE owner_email=%s", (email,))
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
        matches = cur.fetchall()
        _u = matches[0] if len(matches) == 1 else None
        user = dict(_u) if _u else None
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("home"))
        cur.execute("SELECT * FROM listings WHERE owner_email=%s ORDER BY created_at DESC", (user["email"],))
        user_listings = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following_id=%s", (user["id"],))
        follower_count = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower_id=%s", (user["id"],))
        following_count = cur.fetchone()["c"]
        fav_ids = []
        if "email" in session:
            cur.execute("SELECT listing_id FROM favourites WHERE user_email=%s", (session["email"],))
            fav_ids = [r["listing_id"] for r in cur.fetchall()]
        is_following = False
        if "name" in session:
            cur.execute("SELECT 1 FROM follows WHERE follower_id=%s AND following_id=%s",
                        (session["user_id"], user["id"]))
            is_following = bool(cur.fetchone())
    return render_template("public_profile.html", user=user, user_listings=user_listings,
                           follower_count=follower_count, following_count=following_count,
                            fav_ids=fav_ids, is_following=is_following,
                            is_own=session.get("email") == user["email"])

# ── Chat ───────────────────────────────────────────────────────────────────
def chat_sidebar_data(cur, my_id):
    cur.execute("""
        SELECT DISTINCT CASE WHEN sender_id=%s THEN recipient_id ELSE sender_id END AS other_user_id
        FROM messages
        WHERE (sender_id=%s OR recipient_id=%s) AND sender_id IS NOT NULL AND recipient_id IS NOT NULL
    """, (my_id, my_id, my_id))
    other_ids = [row["other_user_id"] for row in cur.fetchall() if row["other_user_id"] is not None]
    cur.execute("SELECT sender_id,COUNT(*) AS cnt FROM messages WHERE recipient_id=%s AND is_read=0 GROUP BY sender_id",
                (my_id,))
    unread_map = {row["sender_id"]: row["cnt"] for row in cur.fetchall()}
    convos = []
    for other_id in other_ids:
        cur.execute("SELECT id,name,username FROM users WHERE id=%s AND banned=0", (other_id,))
        other = cur.fetchone()
        if other:
            convos.append({"other_user_id": other["id"], "other_user": other["name"],
                           "username": other["username"]})
    cur.execute("SELECT id,name,username FROM users WHERE id<>%s AND banned=0 ORDER BY name", (my_id,))
    return convos, unread_map, [dict(row) for row in cur.fetchall()]

@app.route("/chat")
def chat():
    if "email" not in session:
        return redirect(url_for("login"))
    my_id = session["user_id"]
    with get_db() as conn:
        cur = conn.cursor()
        convos, unread_map, users_list = chat_sidebar_data(cur, my_id)
    return render_template("chat.html", convos=convos, unread_map=unread_map,
                           users_list=users_list, active_user=None, active_user_id=None,
                           messages_list=[], my_name=session["name"], my_id=my_id)

@app.route("/chat/u/<int:other_user_id>")
def chat_with(other_user_id):
    if "email" not in session:
        return redirect(url_for("login"))
    my_id = session["user_id"]
    if other_user_id == my_id:
        return redirect(url_for("chat"))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id=%s AND banned=0", (other_user_id,))
        other_row = cur.fetchone()
        if not other_row:
            abort(404)
        other_info = dict(other_row)
        cur.execute("""
            SELECT * FROM messages
            WHERE (sender_id=%s AND recipient_id=%s) OR (sender_id=%s AND recipient_id=%s)
            ORDER BY id DESC LIMIT 200
        """, (my_id, other_user_id, other_user_id, my_id))
        msgs = [dict(r) for r in cur.fetchall()]
        msgs.reverse()
        cur.execute("""
            UPDATE messages SET is_read=1
            WHERE sender_id=%s AND recipient_id=%s AND is_read=0
        """, (other_user_id, my_id))
        conn.commit()
        convos, unread_map, users_list = chat_sidebar_data(cur, my_id)
    return render_template("chat.html", convos=convos, unread_map=unread_map,
                           users_list=users_list, active_user=other_info["name"],
                           active_user_id=other_user_id, messages_list=msgs,
                           other_info=other_info, my_name=session["name"], my_id=my_id)

@app.route("/chat/send", methods=["POST"])
def send_message():
    if "email" not in session:
        return jsonify({"error": "not logged in"}), 401
    if request.content_type and "multipart" in request.content_type:
        recipient_id = request.form.get("recipient_id", type=int)
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
            if not file_name:
                return jsonify({"error": "unsupported or invalid file"}), 400
    else:
        data      = request.get_json(silent=True) or {}
        recipient_id = data.get("recipient_id")
        try:
            recipient_id = int(recipient_id)
        except (TypeError, ValueError):
            recipient_id = None
        body      = str(data.get("body", "")).strip()
        file_name = file_type = ""
    if not recipient_id or recipient_id == session["user_id"] or len(body) > 5000 or (not body and not file_name):
        return jsonify({"error": "missing fields"}), 400
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM users WHERE id=%s AND banned=0", (recipient_id,))
        recipient = cur.fetchone()
        if not recipient:
            return jsonify({"error": "recipient not found"}), 404
        params = (session["user_id"], recipient_id, session["name"], recipient["name"], body,
                  file_name, file_type, datetime.now().strftime("%Y-%m-%d %H:%M"))
        if USE_POSTGRES:
            cur.execute("""INSERT INTO messages (sender_id,recipient_id,sender,recipient,body,file_name,file_type,ts)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", params)
            message_id = cur.fetchone()["id"]
        else:
            cur.execute("""INSERT INTO messages (sender_id,recipient_id,sender,recipient,body,file_name,file_type,ts)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", params)
            message_id = cur.lastrowid
        conn.commit()
    return jsonify({"status": "ok", "id": message_id, "file_name": file_name, "file_type": file_type})


@app.route("/chat/delete/<int:other_user_id>", methods=["POST"])
def delete_chat(other_user_id):
    if "email" not in session:
        return redirect(url_for("login"))
    my_id = session["user_id"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM messages
            WHERE (sender_id=%s AND recipient_id=%s) OR (sender_id=%s AND recipient_id=%s)
        """, (my_id, other_user_id, other_user_id, my_id))
        conn.commit()
    return redirect(url_for("chat"))


@app.route("/chat/unsend/<int:message_id>", methods=["POST"])
def unsend_message(message_id):
    if "email" not in session:
        return jsonify({"error": "not logged in"}), 401
    my_id = session["user_id"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM messages WHERE id=%s", (message_id,))
        msg = cur.fetchone()
        if msg and msg["sender_id"] == my_id:
            cur.execute("DELETE FROM messages WHERE id=%s", (message_id,))
            conn.commit()
            return jsonify({"status": "ok"})
    return jsonify({"error": "not allowed"}), 403


@app.route("/chat/file/<int:message_id>")
def chat_file(message_id):
    if "email" not in session:
        abort(401)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM messages WHERE id=%s", (message_id,))
        msg = cur.fetchone()
    if not msg or session["user_id"] not in (msg["sender_id"], msg["recipient_id"]):
        abort(404)
    allowed = ALLOWED_IMG if msg["file_type"] == "image" else ALLOWED_PDF if msg["file_type"] == "pdf" else set()
    file_url = safe_upload_url(msg["file_name"], allowed)
    if not file_url:
        abort(404)
    if file_url.startswith("/static/uploads/"):
        local_path = os.path.join(app.root_path, file_url.lstrip("/"))
        if not os.path.isfile(local_path):
            abort(404)
        return send_file(local_path, as_attachment=(msg["file_type"] == "pdf"),
                         download_name="document.pdf" if msg["file_type"] == "pdf" else None)
    return redirect(file_url)

@app.route("/chat/poll/<int:other_user_id>")
def poll_messages(other_user_id):
    if "email" not in session:
        return jsonify({"error": "not logged in"}), 401
    my_id  = session["user_id"]
    since_id = max(0, request.args.get("since", 0, type=int) or 0)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM messages
            WHERE ((sender_id=%s AND recipient_id=%s) OR (sender_id=%s AND recipient_id=%s))
            AND id > %s ORDER BY id ASC LIMIT 200
        """, (my_id, other_user_id, other_user_id, my_id, since_id))
        msgs = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "UPDATE messages SET is_read=1 WHERE sender_id=%s AND recipient_id=%s AND is_read=0",
            (other_user_id, my_id)
        )
        conn.commit()
    return jsonify([dict(m) for m in msgs])

# ── Follow ─────────────────────────────────────────────────────────────────
@app.route("/follow/<username>", methods=["POST"])
def follow_user(username):
    if "email" not in session:
        return jsonify({"error": "not logged in"}), 401
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id,name,username FROM users WHERE (name=%s OR username=%s) AND banned=0",
                    (username, username))
        targets = cur.fetchall()
        if len(targets) != 1:
            return jsonify({"error": "user not found"}), 404
        target = targets[0]
        if target["id"] == session["user_id"]:
            return jsonify({"error": "cannot follow yourself"}), 400
        cur.execute("SELECT 1 FROM follows WHERE follower_id=%s AND following_id=%s",
                    (session["user_id"], target["id"]))
        if cur.fetchone():
            cur.execute("DELETE FROM follows WHERE follower_id=%s AND following_id=%s",
                        (session["user_id"], target["id"]))
            action = "unfollowed"
        else:
            cur.execute("INSERT INTO follows (follower_id,following_id,follower,following) VALUES (%s,%s,%s,%s)",
                        (session["user_id"], target["id"], session["name"], target["name"]))
            action = "followed"
        conn.commit()
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following_id=%s", (target["id"],))
        count = cur.fetchone()["c"]
    return jsonify({"action": action, "followers": count})

@app.route("/follow-count/<username>")
def follow_count(username):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE name=%s OR username=%s", (username, username))
        users = cur.fetchall()
        if len(users) != 1:
            return jsonify({"count": 0})
        user = users[0]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following_id=%s", (user["id"],))
        count = cur.fetchone()["c"]
    return jsonify({"count": count})

@app.route("/api/my-follow-counts")
def my_follow_counts():
    if "email" not in session:
        return jsonify({"followers": 0, "following": 0})
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following_id=%s", (session["user_id"],))
        followers = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower_id=%s", (session["user_id"],))
        following = cur.fetchone()["c"]
    return jsonify({"followers": followers, "following": following})

@app.route("/api/followers/<username>")
def get_followers(username):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE name=%s OR username=%s", (username, username))
        users = cur.fetchall()
        if len(users) != 1:
            return jsonify([])
        user = users[0]
        cur.execute("""
            SELECT u.name, u.username, u.avatar FROM users u
            JOIN follows f ON f.follower_id = u.id
            WHERE f.following_id=%s
        """, (user["id"],))
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify(rows)

@app.route("/api/following/<username>")
def get_following(username):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE name=%s OR username=%s", (username, username))
        users = cur.fetchall()
        if len(users) != 1:
            return jsonify([])
        user = users[0]
        cur.execute("""
            SELECT u.name, u.username, u.avatar FROM users u
            JOIN follows f ON f.following_id = u.id
            WHERE f.follower_id=%s
        """, (user["id"],))
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
    data = request.get_json(silent=True) or {}
    lid = data.get("id")
    if not isinstance(lid, str) or len(lid) > 64:
        return jsonify({"error": "invalid listing"}), 400
    email = session["email"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM listings WHERE id=%s", (lid,))
        if not cur.fetchone():
            return jsonify({"error": "listing not found"}), 404
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
        if user and not user["is_admin"]:
            cur.execute("UPDATE users SET banned=%s WHERE id=%s",
                        (0 if user["banned"] else 1, user_id))
            conn.commit()
        elif user:
            flash("Administrator accounts cannot be banned here.", "error")
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
