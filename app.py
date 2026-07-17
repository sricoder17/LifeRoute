import sqlite3, math, os, functools, random, secrets, json, hashlib
from datetime import datetime, timedelta
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, jsonify, Response, send_from_directory

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    import requests as http
except ImportError:
    http = None

# Optional: real automated SOS phone calls via Twilio. Requires a free/paid
# Twilio account. See README for setup — without these environment
# variables set, the SOS button still works but stays a visual-only demo
# broadcast (no call is placed).
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "liferoute.db")
UPLOAD_DIR = os.path.join(APP_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_DOC_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "webp"}

app = Flask(__name__)
app.secret_key = "liferoute-dev-secret-change-me"

# Admin console credentials — override via environment variables in
# production. Defaults are for local/demo use only.
ADMIN_USERNAME = os.environ.get("LR_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("LR_ADMIN_PASSWORD", "admin123")

# Optional: set a real TomTom Traffic Flow API key (free tier available at
# developer.tomtom.com) to replace the built-in heuristic traffic model with
# genuine live traffic data. Everything works without one - it just falls
# back to the time-of-day congestion model below.
TOMTOM_API_KEY = os.environ.get("TOMTOM_API_KEY", "")
OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"

ASSET_CATALOG = {
    "Blood Products": [
        "O-Negative Whole Blood", "O-Positive Whole Blood",
        "A-Negative Whole Blood", "A-Positive Whole Blood",
        "B-Negative Whole Blood", "B-Positive Whole Blood",
        "AB-Negative Whole Blood", "AB-Positive Whole Blood",
        "Fresh Frozen Plasma", "Platelet Concentrate", "Cryoprecipitate",
    ],
    "Critical Equipment": [
        "ECMO Circuit (Portable)", "ICU Ventilator", "Dialysis Machine",
        "Defibrillator", "Infusion Pump", "Oxygen Concentrator",
        "Patient Monitor", "Portable X-Ray Unit", "Intra-Aortic Balloon Pump",
    ],
    "Specialist Physicians": [
        "Vascular Surgeon (On-Call)", "Neurosurgeon (On-Call)",
        "Cardiothoracic Surgeon (On-Call)", "Trauma Surgeon (On-Call)",
        "Anesthesiologist (On-Call)", "Pediatric Intensivist (On-Call)",
        "Interventional Radiologist (On-Call)",
    ],
    "Pharmaceuticals & Antidotes": [
        "Polyvalent Anti-Venom", "Thrombolytic Agent (tPA)",
        "IV Immunoglobulin (IVIG)", "Rabies Immunoglobulin",
        "Botulinum Antitoxin", "Activated Charcoal (Bulk)",
    ],
}
ALL_ASSET_TYPES = [a for group in ASSET_CATALOG.values() for a in group]

TIER_INFO = {
    1: {"label": "Tier 1 · Critical", "window": "<60 min survival window", "speed_kmh": 60},
    2: {"label": "Tier 2 · Intermediate", "window": "2-4 hr survival window", "speed_kmh": 40},
    3: {"label": "Tier 3 · Scheduled", "window": "Baseline logistics", "speed_kmh": 28},
}

STATUS_STEPS = ["pending", "matched", "dispatched", "in_transit", "delivered"]

SUBSCRIPTION_PLANS = {
    "essential":    {"label": "Basic",         "price_inr": 15000, "audience": "Small hospitals & blood banks"},
    "professional": {"label": "Professional",  "price_inr": 30000, "audience": "District hospitals"},
    "enterprise":   {"label": "Enterprise",    "price_inr": 40000, "audience": "Hospital chains"},
}

DOCTORS = [
    {"name": "Dr. Ananya Rao", "phone": "+91 98100 11234", "qualification": "MD, Vascular Surgery"},
    {"name": "Dr. Kabir Malhotra", "phone": "+91 98100 22345", "qualification": "MS, Neurosurgery"},
    {"name": "Dr. Fatima Sheikh", "phone": "+91 98100 33456", "qualification": "MD, Cardiothoracic Surgery"},
    {"name": "Dr. Rohan Verma", "phone": "+91 98100 44567", "qualification": "MD, Trauma & Emergency Medicine"},
    {"name": "Dr. Priya Nair", "phone": "+91 98100 55678", "qualification": "MD, Anesthesiology"},
    {"name": "Dr. Arjun Mehta", "phone": "+91 98100 66789", "qualification": "MD, Pediatric Critical Care"},
    {"name": "Dr. Sara Iqbal", "phone": "+91 98100 77890", "qualification": "MD, Interventional Radiology"},
    {"name": "Dr. Vikram Singh", "phone": "+91 98100 88901", "qualification": "MS, Orthopedic Trauma"},
    {"name": "Dr. Neha Kapoor", "phone": "+91 98100 99012", "qualification": "MD, Hematology & Transfusion Medicine"},
    {"name": "Dr. Imran Chaudhary", "phone": "+91 98100 10123", "qualification": "MD, Toxicology & Critical Care"},
]

# ---------------------------------------------------------------- DB helpers
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS hospitals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        phone TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        plan TEXT DEFAULT 'essential',
        license_number TEXT DEFAULT '',
        document_filename TEXT DEFAULT '',
        verified INTEGER NOT NULL DEFAULT 0,
        document_submitted_at TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS request_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        requester_id INTEGER NOT NULL REFERENCES hospitals(id),
        notes TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
        asset_type TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'Custom',
        quantity INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        UNIQUE(hospital_id, asset_type)
    );
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        requester_id INTEGER NOT NULL REFERENCES hospitals(id),
        asset_type TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        tier INTEGER NOT NULL,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        matched_hospital_id INTEGER REFERENCES hospitals(id),
        group_id INTEGER REFERENCES request_groups(id),
        distance_km REAL,
        eta_min REAL,
        created_at TEXT NOT NULL,
        matched_at TEXT,
        dispatched_at TEXT,
        transit_at TEXT,
        delivered_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
        created_at TEXT NOT NULL
    );
    """)
    db.commit()

    # ---- migration: new routing/traffic columns for pre-existing DBs ----
    for col, ddl in [
        ("route_geojson",     "ALTER TABLE requests ADD COLUMN route_geojson TEXT"),
        ("base_duration_min", "ALTER TABLE requests ADD COLUMN base_duration_min REAL"),
        ("traffic_level",     "ALTER TABLE requests ADD COLUMN traffic_level TEXT"),
        ("traffic_multiplier","ALTER TABLE requests ADD COLUMN traffic_multiplier REAL"),
        ("route_source",      "ALTER TABLE requests ADD COLUMN route_source TEXT"),
        ("group_id",          "ALTER TABLE requests ADD COLUMN group_id INTEGER"),
        ("plan",              "ALTER TABLE hospitals ADD COLUMN plan TEXT DEFAULT 'essential'"),
        ("license_number",    "ALTER TABLE hospitals ADD COLUMN license_number TEXT DEFAULT ''"),
        ("document_filename", "ALTER TABLE hospitals ADD COLUMN document_filename TEXT DEFAULT ''"),
        ("verified",          "ALTER TABLE hospitals ADD COLUMN verified INTEGER NOT NULL DEFAULT 0"),
        ("document_submitted_at", "ALTER TABLE hospitals ADD COLUMN document_submitted_at TEXT DEFAULT ''"),
    ]:
        try:
            db.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    db.commit()

    cur = db.execute("SELECT COUNT(*) AS c FROM hospitals")
    if cur.fetchone()["c"] == 0:
        demo_hospitals = [
            ("St. Xavier General Hospital", "stxavier", "demo123", 28.6139, 77.2090, "+91-11-4000-1001", "DL-HOSP-2021-01147"),
            ("North Ridge Trauma Center",   "northridge", "demo123", 28.7041, 77.1025, "+91-11-4000-1002", "DL-HOSP-2022-00893"),
            ("Delhi Cantt Base Hospital",   "cantt",      "demo123", 28.5921, 77.1503, "+91-11-4000-1003", "DL-HOSP-2020-00256"),
            ("Rohini Sector-9 Medical Center", "rohini",  "demo123", 28.7495, 77.0565, "+91-11-4000-1004", "DL-HOSP-2023-01590"),
            ("Greater Kailash Care Hospital", "gk",       "demo123", 28.5480, 77.2437, "+91-11-4000-1005", "DL-HOSP-2021-00734"),
        ]
        now = datetime.utcnow().isoformat()
        for i, (name, uname, pw, lat, lon, phone, license_number) in enumerate(demo_hospitals):
            # Backdate each demo hospital's registration + document submission
            # by a different amount so the "renew in 3 years" logic has
            # realistic, varied examples to show off.
            submitted_dt = datetime.utcnow() - timedelta(days=[400, 730, 1050, 120, 900][i])
            submitted_at = submitted_dt.isoformat()
            doc_filename = f"{uname}_license.txt"
            with open(os.path.join(UPLOAD_DIR, doc_filename), "w") as f:
                f.write(
                    f"LifeRoute Demo Registration Document\n"
                    f"--------------------------------------\n"
                    f"Hospital: {name}\n"
                    f"License number: {license_number}\n"
                    f"Submitted: {submitted_dt.strftime('%d %b %Y')}\n"
                    f"Status: Verified by LifeRoute Admin\n\n"
                    f"This is a placeholder document for demo account '{uname}'.\n"
                )
            db.execute(
                "INSERT INTO hospitals (name, username, password_hash, lat, lon, phone, created_at, "
                "license_number, document_filename, verified, document_submitted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, uname, generate_password_hash(pw), lat, lon, phone, submitted_at,
                 license_number, doc_filename, 1, submitted_at)
            )
        db.commit()
        rows = db.execute("SELECT id FROM hospitals").fetchall()
        random.seed(42)
        inv_now = datetime.utcnow().isoformat()
        for r in rows:
            for category, assets in ASSET_CATALOG.items():
                for asset in assets:
                    qty = random.choice([0, 0, 1, 2, 3, 4, 6, 8])
                    db.execute(
                        "INSERT INTO inventory (hospital_id, asset_type, category, quantity, updated_at) VALUES (?,?,?,?,?)",
                        (r["id"], asset, category, qty, inv_now)
                    )
        db.commit()
    db.close()

# ---------------------------------------------------------------- utils
def compute_reliability(db, hospital_id):
    """Network reliability score for a hospital acting as a supplier:
    the share of requests assigned to it that were actually delivered.
    Hospitals with no supply history yet default to a neutral 100."""
    row = db.execute(
        "SELECT COUNT(*) AS assigned, "
        "SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) AS delivered "
        "FROM requests WHERE matched_hospital_id=?",
        (hospital_id,)
    ).fetchone()
    assigned = row["assigned"] or 0
    delivered = row["delivered"] or 0
    if assigned == 0:
        return {"score": 100, "assigned": 0, "delivered": 0, "band": "new"}
    score = round(100 * delivered / assigned)
    band = "high" if score >= 90 else ("mid" if score >= 70 else "low")
    return {"score": score, "assigned": assigned, "delivered": delivered, "band": band}

def document_renewal_info(document_submitted_at):
    """Given an ISO submission timestamp, return the 3-year renewal due
    date plus a status flag: 'ok' | 'due_soon' (<=90 days) | 'overdue'."""
    if not document_submitted_at:
        return None
    try:
        submitted = datetime.fromisoformat(document_submitted_at)
    except ValueError:
        return None
    due = submitted.replace(year=submitted.year + 3)
    days_left = (due - datetime.utcnow()).days
    if days_left < 0:
        status = "overdue"
    elif days_left <= 90:
        status = "due_soon"
    else:
        status = "ok"
    return {"submitted": submitted, "due": due, "days_left": days_left, "status": status}

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(math.sqrt(a))

# ---------------------------------------------------------------- routing + traffic engine
def fetch_road_route(lat1, lon1, lat2, lon2):
    """
    Ask OSRM's public routing server for the actual road-following route
    between two points (real streets/highways, not a straight line).
    Returns dict(coords=[[lat,lon],...], distance_km, duration_min, source)
    Falls back to a straight line + haversine distance if OSRM is unreachable
    (no internet in this sandbox, rate-limited, offline demo, etc).
    """
    if http is not None:
        try:
            url = f"{OSRM_BASE}/{lon1},{lat1};{lon2},{lat2}"
            r = http.get(url, params={"overview": "full", "geometries": "geojson"}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == "Ok" and data.get("routes"):
                    route = data["routes"][0]
                    coords = [[c[1], c[0]] for c in route["geometry"]["coordinates"]]
                    return {
                        "coords": coords,
                        "distance_km": route["distance"] / 1000.0,
                        "duration_min": route["duration"] / 60.0,
                        "source": "osrm",
                    }
        except Exception:
            pass
    # fallback: straight line
    d = haversine_km(lat1, lon1, lat2, lon2)
    return {
        "coords": [[lat1, lon1], [lat2, lon2]],
        "distance_km": d,
        "duration_min": (d / 40.0) * 60.0,
        "source": "straight_line",
    }

def live_traffic_multiplier(lat, lon, seed_key=""):
    """
    Returns (level, multiplier, source).
    If TOMTOM_API_KEY is configured, queries TomTom's real-time Traffic Flow
    API (currentSpeed vs freeFlowSpeed at the route midpoint) for genuine
    live traffic. Otherwise falls back to a time-of-day congestion heuristic,
    lightly jittered per-route so the network view feels alive.
    """
    if TOMTOM_API_KEY and http is not None:
        try:
            url = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
            r = http.get(url, params={"point": f"{lat},{lon}", "key": TOMTOM_API_KEY}, timeout=4)
            if r.status_code == 200:
                seg = r.json().get("flowSegmentData", {})
                cur, free = seg.get("currentSpeed"), seg.get("freeFlowSpeed")
                if cur and free:
                    ratio = free / max(cur, 1)
                    ratio = max(0.85, min(ratio, 2.2))
                    level = "Heavy" if ratio > 1.5 else ("Moderate" if ratio > 1.15 else "Light")
                    return level, round(ratio, 2), "tomtom_live"
        except Exception:
            pass

    # Heuristic fallback: approximate local time (IST) rush-hour congestion,
    # with a small deterministic jitter so it drifts slightly over time
    # instead of being perfectly static.
    local = datetime.utcnow() + timedelta(hours=5, minutes=30)
    hour = local.hour + local.minute / 60.0
    if 7.5 <= hour < 10.5 or 17 <= hour < 20.5:
        base_level, base_mult = "Heavy", 1.55
    elif 10.5 <= hour < 17:
        base_level, base_mult = "Moderate", 1.18
    elif 20.5 <= hour < 23:
        base_level, base_mult = "Moderate", 1.10
    else:
        base_level, base_mult = "Light", 0.92

    jitter_seed = f"{seed_key}-{local.strftime('%Y%m%d%H')}"
    jitter = (int(hashlib.md5(jitter_seed.encode()).hexdigest()[:6], 16) % 21 - 10) / 100.0  # -0.10..+0.10
    mult = max(0.8, round(base_mult + jitter, 2))
    return base_level, mult, "heuristic"

AVG_SPEED_KMH = 75.0

def build_route_and_eta(origin_lat, origin_lon, dest_lat, dest_lon, tier, seed_key=""):
    """
    Full pipeline: real road route -> midpoint live traffic (shown as an
    informational badge only) -> ETA = distance / average speed (75 km/h).
    """
    route = fetch_road_route(origin_lat, origin_lon, dest_lat, dest_lon)
    mid_idx = len(route["coords"]) // 2
    mid_lat, mid_lon = route["coords"][mid_idx]
    level, mult, tsource = live_traffic_multiplier(mid_lat, mid_lon, seed_key=seed_key)

    eta_min = (route["distance_km"] / AVG_SPEED_KMH) * 60.0

    return {
        "coords": route["coords"],
        "distance_km": route["distance_km"],
        "route_source": route["source"],
        "base_duration_min": round(eta_min, 1),
        "eta_min": round(eta_min, 1),
        "traffic_level": level,
        "traffic_multiplier": mult,
        "traffic_source": tsource,
    }

# ---- multi-tab token auth -------------------------------------------------
# Each browser tab keeps its own token (in sessionStorage, which is NOT
# shared between tabs) instead of one shared login cookie. That means two
# different hospital accounts can be logged in at the same time in two tabs
# of the same browser, and refreshing one tab never bumps the other's login.
def create_session_token(hospital_id):
    token = secrets.token_urlsafe(24)
    db = get_db()
    db.execute("INSERT INTO sessions (token, hospital_id, created_at) VALUES (?,?,?)",
               (token, hospital_id, datetime.utcnow().isoformat()))
    db.commit()
    return token

def get_request_token():
    return request.values.get("st") or request.headers.get("X-LR-Token") or ""

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        token = get_request_token()
        row = None
        if token:
            row = get_db().execute(
                "SELECT hospital_id FROM sessions WHERE token=?", (token,)
            ).fetchone()
        if not row:
            return redirect(url_for("login"))
        g.hospital_id = row["hospital_id"]
        g.token = token
        return view(*args, **kwargs)
    return wrapped

def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped

def current_hospital():
    hid = getattr(g, "hospital_id", None)
    if not hid:
        return None
    return get_db().execute("SELECT * FROM hospitals WHERE id=?", (hid,)).fetchone()

@app.context_processor
def inject_token():
    return {"st_token": getattr(g, "token", ""), "me": current_hospital()}

# ---------------------------------------------------------------- auth routes
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        db = get_db()
        name = request.form["name"].strip()
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        phone = request.form.get("phone", "").strip()
        lat = float(request.form["lat"])
        lon = float(request.form["lon"])
        license_number = request.form.get("license_number", "").strip()
        plan = request.form.get("plan", "essential")
        if plan not in SUBSCRIPTION_PLANS:
            plan = "essential"
        if not name or not username or not password or not license_number:
            flash("All fields are required, including your hospital license number.", "error")
            return render_template("register.html", plans=SUBSCRIPTION_PLANS, selected_plan=plan)
        exists = db.execute("SELECT id FROM hospitals WHERE username=?", (username,)).fetchone()
        if exists:
            flash("That username is already registered.", "error")
            return render_template("register.html", plans=SUBSCRIPTION_PLANS, selected_plan=plan)

        # Handle the registration document upload (license/accreditation
        # proof). Optional at the form level, but flagged to admins if
        # missing so they know to follow up.
        document_filename = ""
        document_submitted_at = ""
        doc_file = request.files.get("document")
        if doc_file and doc_file.filename:
            ext = doc_file.filename.rsplit(".", 1)[-1].lower() if "." in doc_file.filename else ""
            if ext not in ALLOWED_DOC_EXTENSIONS:
                flash("Document must be a PDF, JPG, PNG or WEBP file.", "error")
                return render_template("register.html", plans=SUBSCRIPTION_PLANS, selected_plan=plan)
            safe_base = secure_filename(f"{username}_{secrets.token_hex(4)}.{ext}")
            doc_file.save(os.path.join(UPLOAD_DIR, safe_base))
            document_filename = safe_base
            document_submitted_at = datetime.utcnow().isoformat()

        now = datetime.utcnow().isoformat()
        cur = db.execute(
            "INSERT INTO hospitals (name, username, password_hash, lat, lon, phone, created_at, plan, "
            "license_number, document_filename, document_submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, username, generate_password_hash(password), lat, lon, phone, now, plan,
             license_number, document_filename, document_submitted_at)
        )
        hid = cur.lastrowid
        for category, assets in ASSET_CATALOG.items():
            for asset in assets:
                db.execute(
                    "INSERT INTO inventory (hospital_id, asset_type, category, quantity, updated_at) VALUES (?,?,?,0,?)",
                    (hid, asset, category, now)
                )
        db.commit()
        token = create_session_token(hid)
        return redirect(url_for("payment_portal", hospital_id=hid, plan=plan, st=token))
    plan = request.args.get("plan", "essential")
    return render_template("register.html", plans=SUBSCRIPTION_PLANS, selected_plan=plan)

@app.route("/payment")
def payment_portal():
    hospital_id = request.args.get("hospital_id", type=int)
    plan = request.args.get("plan", "essential")
    if plan not in SUBSCRIPTION_PLANS or not hospital_id:
        return redirect(url_for("register"))
    h = get_db().execute("SELECT name FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not h:
        return redirect(url_for("register"))
    return render_template("payment.html", plan_key=plan, plan=SUBSCRIPTION_PLANS[plan],
                            hospital_id=hospital_id, hospital_name=h["name"])

@app.route("/payment/confirm", methods=["POST"])
def payment_confirm():
    # Demo-only mock checkout — no real payment provider is connected. In
    # production this is where you'd verify a Razorpay/Stripe webhook
    # before activating the subscription.
    hospital_id = request.form.get("hospital_id", type=int)
    plan = request.form.get("plan", "essential")
    if hospital_id and plan in SUBSCRIPTION_PLANS:
        get_db().execute("UPDATE hospitals SET plan=? WHERE id=?", (plan, hospital_id))
        get_db().commit()
        flash(f"Payment successful. Your {SUBSCRIPTION_PLANS[plan]['label']} subscription is now active.", "success")
    token = get_request_token()
    return redirect(url_for("dashboard", st=token) if token else url_for("login"))

@app.route("/subscription")
@login_required
def subscription():
    me = current_hospital()
    return render_template("subscription.html", me=me, plans=SUBSCRIPTION_PLANS)

@app.route("/subscription/change", methods=["POST"])
@login_required
def subscription_change():
    plan = request.form.get("plan", "essential")
    if plan in SUBSCRIPTION_PLANS:
        db = get_db()
        db.execute("UPDATE hospitals SET plan=? WHERE id=?", (plan, g.hospital_id))
        db.commit()
        flash(f"Switched to the {SUBSCRIPTION_PLANS[plan]['label']} plan.", "success")
    return redirect(url_for("subscription", st=g.token))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db = get_db()
        username = request.form["username"].strip().lower()
        password = request.form["password"]
        row = db.execute("SELECT * FROM hospitals WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            token = create_session_token(row["id"])
            return redirect(url_for("dashboard", st=token))
        flash("Invalid username or password.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    token = get_request_token()
    if token:
        db = get_db()
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        db.commit()
    session.clear()
    return redirect(url_for("login"))

# ---------------------------------------------------------------- admin console
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid admin credentials.", "error")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    tab = request.args.get("tab", "documentation")
    if tab not in ("documentation", "inventory", "requests", "reliability"):
        tab = "documentation"

    hospitals = db.execute("SELECT * FROM hospitals ORDER BY created_at DESC").fetchall()

    rows = []
    for h in hospitals:
        inv = db.execute(
            "SELECT COALESCE(SUM(quantity),0) AS units, COUNT(*) AS lines, "
            "SUM(CASE WHEN quantity=0 THEN 1 ELSE 0 END) AS zero_lines "
            "FROM inventory WHERE hospital_id=?",
            (h["id"],)
        ).fetchone()

        asked = db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) AS delivered, "
            "SUM(CASE WHEN status NOT IN ('delivered','unmatched') THEN 1 ELSE 0 END) AS pending "
            "FROM requests WHERE requester_id=?",
            (h["id"],)
        ).fetchone()

        reliability = compute_reliability(db, h["id"])
        renewal = document_renewal_info(h["document_submitted_at"])

        rows.append({
            "hospital": h,
            "plan_label": SUBSCRIPTION_PLANS.get(h["plan"] or "essential", {}).get("label", h["plan"]),
            "inventory_units": inv["units"],
            "inventory_lines": inv["lines"],
            "inventory_zero_lines": inv["zero_lines"] or 0,
            "asked_total": asked["total"] or 0,
            "asked_delivered": asked["delivered"] or 0,
            "asked_pending": asked["pending"] or 0,
            "reliability": reliability,
            "renewal": renewal,
        })

    totals = {
        "hospitals": len(rows),
        "fulfilled": sum(r["reliability"]["delivered"] for r in rows),
        "inventory_units": sum(r["inventory_units"] for r in rows),
        "missing_docs": sum(1 for r in rows if not r["hospital"]["document_filename"]),
        "unverified": sum(1 for r in rows if not r["hospital"]["verified"]),
        "avg_reliability": round(sum(r["reliability"]["score"] for r in rows) / len(rows)) if rows else 100,
    }
    return render_template("admin_dashboard.html", rows=rows, totals=totals, tab=tab)

@app.route("/admin/verify/<int:hospital_id>", methods=["POST"])
@admin_required
def admin_toggle_verify(hospital_id):
    db = get_db()
    h = db.execute("SELECT verified FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if h is not None:
        db.execute("UPDATE hospitals SET verified=? WHERE id=?", (0 if h["verified"] else 1, hospital_id))
        db.commit()
    return redirect(url_for("admin_dashboard", tab=request.args.get("tab", "documentation")))

@app.route("/admin/download/<int:hospital_id>")
@admin_required
def admin_download_document(hospital_id):
    db = get_db()
    h = db.execute("SELECT name, document_filename FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if h is None or not h["document_filename"]:
        flash("No document on file for that hospital.", "error")
        return redirect(url_for("admin_dashboard", tab="documentation"))
    filepath = os.path.join(UPLOAD_DIR, h["document_filename"])
    if not os.path.isfile(filepath):
        flash("Document file is missing from storage.", "error")
        return redirect(url_for("admin_dashboard", tab="documentation"))
    ext = h["document_filename"].rsplit(".", 1)[-1]
    download_name = f"{secure_filename(h['name'])}_license.{ext}"
    return send_from_directory(UPLOAD_DIR, h["document_filename"], as_attachment=True, download_name=download_name)

# ---------------------------------------------------------------- core routes
@app.route("/")
def index():
    token = get_request_token()
    if token and get_db().execute("SELECT 1 FROM sessions WHERE token=?", (token,)).fetchone():
        return redirect(url_for("dashboard", st=token))
    return render_template("landing.html", plans=SUBSCRIPTION_PLANS)

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    me = current_hospital()
    inventory = db.execute(
        "SELECT * FROM inventory WHERE hospital_id=? ORDER BY category, asset_type", (me["id"],)
    ).fetchall()
    grouped = {}
    for item in inventory:
        grouped.setdefault(item["category"], []).append(item)

    outgoing = db.execute(
        "SELECT r.*, h.name AS matched_name FROM requests r "
        "LEFT JOIN hospitals h ON h.id = r.matched_hospital_id "
        "WHERE r.requester_id=? ORDER BY r.created_at DESC LIMIT 15", (me["id"],)
    ).fetchall()
    incoming = db.execute(
        "SELECT r.*, h.name AS requester_name FROM requests r "
        "JOIN hospitals h ON h.id = r.requester_id "
        "WHERE r.matched_hospital_id=? ORDER BY r.created_at DESC LIMIT 15", (me["id"],)
    ).fetchall()
    active_count = db.execute(
        "SELECT COUNT(*) c FROM requests WHERE (requester_id=? OR matched_hospital_id=?) AND status NOT IN ('delivered','unmatched')",
        (me["id"], me["id"])
    ).fetchone()["c"]
    delivered_count = db.execute(
        "SELECT COUNT(*) c FROM requests WHERE (requester_id=? OR matched_hospital_id=?) AND status='delivered'",
        (me["id"], me["id"])
    ).fetchone()["c"]
    low_stock = db.execute(
        "SELECT COUNT(*) c FROM inventory WHERE hospital_id=? AND quantity=0", (me["id"],)
    ).fetchone()["c"]
    network_count = db.execute("SELECT COUNT(*) c FROM hospitals").fetchone()["c"]

    return render_template("dashboard.html", me=me, grouped=grouped,
                            outgoing=outgoing, incoming=incoming,
                            network_count=network_count, active_count=active_count,
                            delivered_count=delivered_count, low_stock=low_stock,
                            tier_info=TIER_INFO)

@app.route("/documents")
@login_required
def documents():
    me = current_hospital()
    renewal = document_renewal_info(me["document_submitted_at"])
    return render_template("documents.html", me=me, renewal=renewal)

@app.route("/inventory")
@login_required
def inventory_page():
    db = get_db()
    me = current_hospital()
    inventory = db.execute(
        "SELECT * FROM inventory WHERE hospital_id=? ORDER BY category, asset_type", (me["id"],)
    ).fetchall()
    grouped = {}
    for item in inventory:
        grouped.setdefault(item["category"], []).append(item)
    total_units = sum(i["quantity"] for i in inventory)
    zero_count = sum(1 for i in inventory if i["quantity"] == 0)
    low_count = sum(1 for i in inventory if 0 < i["quantity"] <= 2)
    return render_template("inventory.html", me=me, grouped=grouped,
                            total_units=total_units, zero_count=zero_count,
                            low_count=low_count, total_types=len(inventory))

@app.route("/inventory/update", methods=["POST"])
@login_required
def inventory_update():
    db = get_db()
    me = current_hospital()
    asset_type = request.form["asset_type"]
    quantity = max(0, int(request.form["quantity"]))
    now = datetime.utcnow().isoformat()
    db.execute(
        "UPDATE inventory SET quantity=?, updated_at=? WHERE hospital_id=? AND asset_type=?",
        (quantity, now, me["id"], asset_type)
    )
    db.commit()
    return jsonify({"ok": True, "asset_type": asset_type, "quantity": quantity})

@app.route("/inventory/add-custom", methods=["POST"])
@login_required
def inventory_add_custom():
    db = get_db()
    me = current_hospital()
    name = request.form["name"].strip()
    quantity = max(0, int(request.form.get("quantity", 0)))
    now = datetime.utcnow().isoformat()
    if name:
        existing = db.execute(
            "SELECT id FROM inventory WHERE hospital_id=? AND asset_type=?", (me["id"], name)
        ).fetchone()
        if existing:
            flash(f'"{name}" already exists in your inventory.', "error")
        else:
            db.execute(
                "INSERT INTO inventory (hospital_id, asset_type, category, quantity, updated_at) VALUES (?,?,?,?,?)",
                (me["id"], name, "Custom", quantity, now)
            )
            db.commit()
            flash(f'Added custom asset "{name}" to your inventory.', "success")
    return redirect(url_for("dashboard", st=g.token))

@app.route("/request/new", methods=["GET", "POST"])
@login_required
def new_request():
    me = current_hospital()
    db = get_db()
    if request.method == "POST":
        try:
            items = json.loads(request.form.get("items_json", "[]"))
        except Exception:
            items = []
        notes = request.form.get("notes", "").strip()
        now = datetime.utcnow().isoformat()

        # normalize + validate cart lines
        clean_items = []
        for it in items:
            asset_type = (it.get("asset_type") or "").strip()
            if it.get("asset_type") == "__custom__":
                asset_type = (it.get("custom_asset") or "").strip()
            try:
                quantity = max(1, int(it.get("quantity", 1)))
                tier = int(it.get("tier", 2))
            except (TypeError, ValueError):
                continue
            if asset_type and tier in TIER_INFO:
                clean_items.append({"asset_type": asset_type, "quantity": quantity, "tier": tier})

        if not clean_items:
            flash("Please add at least one item to your request.", "error")
            return redirect(url_for("new_request", st=g.token))

        group_cur = db.execute(
            "INSERT INTO request_groups (requester_id, notes, created_at) VALUES (?,?,?)",
            (me["id"], notes, now)
        )
        group_id = group_cur.lastrowid

        # ---- Step 1: can ONE hospital fulfil the whole cart? ----
        candidate_sets = []
        for it in clean_items:
            rows = db.execute(
                "SELECT hospital_id FROM inventory WHERE asset_type=? AND quantity>=? AND hospital_id!=?",
                (it["asset_type"], it["quantity"], me["id"])
            ).fetchall()
            candidate_sets.append({r["hospital_id"] for r in rows})
        common_hospitals = set.intersection(*candidate_sets) if candidate_sets else set()

        req_ids = []
        if common_hospitals:
            # rank the common hospitals by straight-line distance, then pick the
            # one with the best real, traffic-aware travel time for this cart
            hrows = db.execute(
                f"SELECT id,name,lat,lon FROM hospitals WHERE id IN ({','.join('?'*len(common_hospitals))})",
                tuple(common_hospitals)
            ).fetchall()
            by_air = sorted(hrows, key=lambda h: haversine_km(me["lat"], me["lon"], h["lat"], h["lon"]))[:4]
            worst_tier = min(it["tier"] for it in clean_items)  # fastest/most critical tier drives ETA
            routed = [(build_route_and_eta(h["lat"], h["lon"], me["lat"], me["lon"], worst_tier,
                                            seed_key=f"grp{group_id}-h{h['id']}"), h) for h in by_air]
            routed.sort(key=lambda x: x[0]["eta_min"])
            info, winner = routed[0]

            for it in clean_items:
                db.execute(
                    "UPDATE inventory SET quantity=quantity-?, updated_at=? WHERE hospital_id=? AND asset_type=?",
                    (it["quantity"], now, winner["id"], it["asset_type"])
                )
                cur = db.execute(
                    "INSERT INTO requests (group_id, requester_id, asset_type, quantity, tier, notes, status, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (group_id, me["id"], it["asset_type"], it["quantity"], it["tier"], notes, "pending", now)
                )
                req_ids.append(cur.lastrowid)
                db.execute(
                    "UPDATE requests SET status='matched', matched_hospital_id=?, distance_km=?, eta_min=?, matched_at=?, "
                    "route_geojson=?, base_duration_min=?, traffic_level=?, traffic_multiplier=?, route_source=? WHERE id=?",
                    (winner["id"], round(info["distance_km"], 2), info["eta_min"], now, json.dumps(info["coords"]),
                     info["base_duration_min"], info["traffic_level"], info["traffic_multiplier"], info["route_source"], req_ids[-1])
                )
            db.commit()
            flash(f'All {len(clean_items)} item(s) matched to a single hospital: {winner["name"]}.', "success")
        else:
            # ---- Step 2: split smartly - match each item to its own best hospital(s) ----
            matched_n, partial_n, unmatched_n = 0, 0, 0
            for it in clean_items:
                # try a single hospital that can cover the whole line first
                rows = db.execute(
                    "SELECT h.id, h.name, h.lat, h.lon FROM hospitals h JOIN inventory i ON i.hospital_id=h.id "
                    "WHERE i.asset_type=? AND i.quantity>=? AND h.id!=?",
                    (it["asset_type"], it["quantity"], me["id"])
                ).fetchall()

                if rows:
                    # single hospital covers the full line - same as before
                    cur = db.execute(
                        "INSERT INTO requests (group_id, requester_id, asset_type, quantity, tier, notes, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (group_id, me["id"], it["asset_type"], it["quantity"], it["tier"], notes, "pending", now)
                    )
                    req_id = cur.lastrowid
                    req_ids.append(req_id)

                    shortlist = sorted(rows, key=lambda h: haversine_km(me["lat"], me["lon"], h["lat"], h["lon"]))[:4]
                    routed = [(build_route_and_eta(h["lat"], h["lon"], me["lat"], me["lon"], it["tier"],
                                                    seed_key=f"req{req_id}-h{h['id']}"), h) for h in shortlist]
                    routed.sort(key=lambda x: x[0]["eta_min"])
                    info, winner = routed[0]
                    db.execute(
                        "UPDATE inventory SET quantity=quantity-?, updated_at=? WHERE hospital_id=? AND asset_type=?",
                        (it["quantity"], now, winner["id"], it["asset_type"])
                    )
                    db.execute(
                        "UPDATE requests SET status='matched', matched_hospital_id=?, distance_km=?, eta_min=?, matched_at=?, "
                        "route_geojson=?, base_duration_min=?, traffic_level=?, traffic_multiplier=?, route_source=? WHERE id=?",
                        (winner["id"], round(info["distance_km"], 2), info["eta_min"], now, json.dumps(info["coords"]),
                         info["base_duration_min"], info["traffic_level"], info["traffic_multiplier"], info["route_source"], req_id)
                    )
                    matched_n += 1
                    continue

                # no single hospital has enough - pool stock from multiple hospitals,
                # nearest/fastest first, until the requested quantity is covered
                pool_rows = db.execute(
                    "SELECT h.id, h.name, h.lat, h.lon, i.quantity AS available FROM hospitals h "
                    "JOIN inventory i ON i.hospital_id=h.id "
                    "WHERE i.asset_type=? AND i.quantity>0 AND h.id!=?",
                    (it["asset_type"], me["id"])
                ).fetchall()

                if not pool_rows:
                    cur = db.execute(
                        "INSERT INTO requests (group_id, requester_id, asset_type, quantity, tier, notes, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (group_id, me["id"], it["asset_type"], it["quantity"], it["tier"], notes, "unmatched", now)
                    )
                    req_ids.append(cur.lastrowid)
                    unmatched_n += 1
                    continue

                shortlist = sorted(pool_rows, key=lambda h: haversine_km(me["lat"], me["lon"], h["lat"], h["lon"]))[:6]
                routed = sorted(
                    [(build_route_and_eta(h["lat"], h["lon"], me["lat"], me["lon"], it["tier"],
                                           seed_key=f"grp{group_id}-h{h['id']}-{it['asset_type']}"), h)
                     for h in shortlist],
                    key=lambda x: x[0]["eta_min"]
                )

                remaining = it["quantity"]
                contributors = []
                for info, h in routed:
                    if remaining <= 0:
                        break
                    take = min(h["available"], remaining)
                    if take <= 0:
                        continue
                    contributors.append((info, h, take))
                    remaining -= take

                fulfilled_qty = it["quantity"] - remaining
                for info, winner, take in contributors:
                    cur = db.execute(
                        "INSERT INTO requests (group_id, requester_id, asset_type, quantity, tier, notes, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (group_id, me["id"], it["asset_type"], take, it["tier"], notes, "pending", now)
                    )
                    req_id = cur.lastrowid
                    req_ids.append(req_id)
                    db.execute(
                        "UPDATE inventory SET quantity=quantity-?, updated_at=? WHERE hospital_id=? AND asset_type=?",
                        (take, now, winner["id"], it["asset_type"])
                    )
                    db.execute(
                        "UPDATE requests SET status='matched', matched_hospital_id=?, distance_km=?, eta_min=?, matched_at=?, "
                        "route_geojson=?, base_duration_min=?, traffic_level=?, traffic_multiplier=?, route_source=? WHERE id=?",
                        (winner["id"], round(info["distance_km"], 2), info["eta_min"], now, json.dumps(info["coords"]),
                         info["base_duration_min"], info["traffic_level"], info["traffic_multiplier"], info["route_source"], req_id)
                    )

                if remaining > 0:
                    # log the still-short balance as its own unmatched line so it's visible on the dashboard
                    cur = db.execute(
                        "INSERT INTO requests (group_id, requester_id, asset_type, quantity, tier, notes, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (group_id, me["id"], it["asset_type"], remaining, it["tier"], notes, "unmatched", now)
                    )
                    req_ids.append(cur.lastrowid)

                if fulfilled_qty >= it["quantity"]:
                    matched_n += 1
                elif fulfilled_qty > 0:
                    partial_n += 1
                else:
                    unmatched_n += 1

            db.commit()
            bits = []
            if matched_n: bits.append(f"{matched_n} item(s) fully matched")
            if partial_n: bits.append(f"{partial_n} item(s) split across multiple hospitals to cover the full quantity")
            if unmatched_n: bits.append(f"{unmatched_n} item(s) could not be matched right now")
            flash("; ".join(bits) + ".", "error" if (matched_n == 0 and partial_n == 0) else "success")

        return redirect(url_for("request_detail", req_id=req_ids[0], st=g.token))

    return render_template("new_request.html", catalog=ASSET_CATALOG, tier_info=TIER_INFO)

@app.route("/request/<int:req_id>")
@login_required
def request_detail(req_id):
    db = get_db()
    me = current_hospital()
    r = db.execute(
        "SELECT r.*, h1.name AS requester_name, h1.lat AS r_lat, h1.lon AS r_lon, h1.phone AS requester_phone, "
        "h2.name AS matched_name, h2.lat AS m_lat, h2.lon AS m_lon, h2.phone AS matched_phone "
        "FROM requests r "
        "JOIN hospitals h1 ON h1.id = r.requester_id "
        "LEFT JOIN hospitals h2 ON h2.id = r.matched_hospital_id "
        "WHERE r.id=?", (req_id,)
    ).fetchone()
    if r is None or (r["requester_id"] != me["id"] and r["matched_hospital_id"] != me["id"]):
        flash("Request not found or access denied.", "error")
        return redirect(url_for("dashboard", st=g.token))

    route_coords = json.loads(r["route_geojson"]) if r["route_geojson"] else None
    live_level, live_mult, live_source = (r["traffic_level"], r["traffic_multiplier"], None)
    if r["matched_hospital_id"] and route_coords:
        mid = route_coords[len(route_coords)//2]
        live_level, live_mult, live_source = live_traffic_multiplier(mid[0], mid[1], seed_key=f"req{r['id']}")

    siblings = []
    if r["group_id"]:
        siblings = db.execute(
            "SELECT r2.*, h2.name AS matched_name FROM requests r2 "
            "LEFT JOIN hospitals h2 ON h2.id = r2.matched_hospital_id "
            "WHERE r2.group_id=? AND r2.id != ? ORDER BY r2.id", (r["group_id"], r["id"])
        ).fetchall()

    return render_template("request_detail.html", r=r, me=me, tier_info=TIER_INFO, status_steps=STATUS_STEPS,
                            route_coords=route_coords, live_traffic_level=live_level,
                            live_traffic_mult=live_mult, live_traffic_source=live_source, siblings=siblings)

@app.route("/api/request/<int:req_id>")
@login_required
def api_request_status(req_id):
    db = get_db()
    me = current_hospital()
    r = db.execute(
        "SELECT r.*, h1.name AS requester_name, h1.lat AS r_lat, h1.lon AS r_lon, "
        "h2.name AS matched_name, h2.lat AS m_lat, h2.lon AS m_lon "
        "FROM requests r "
        "JOIN hospitals h1 ON h1.id = r.requester_id "
        "LEFT JOIN hospitals h2 ON h2.id = r.matched_hospital_id "
        "WHERE r.id=?", (req_id,)
    ).fetchone()
    if r is None or (r["requester_id"] != me["id"] and r["matched_hospital_id"] != me["id"]):
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(r))

def advance_status(req_id, expected_current, new_status, field, actor="matched"):
    db = get_db()
    me = current_hospital()
    r = db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if r is None:
        flash("Request not found.", "error")
        return
    if actor == "matched" and r["matched_hospital_id"] != me["id"]:
        flash("You are not the fulfilling hospital for this request.", "error")
        return
    if actor == "requester" and r["requester_id"] != me["id"]:
        flash("You are not the requesting hospital for this request.", "error")
        return
    if r["status"] != expected_current:
        flash(f"Request is not in '{expected_current}' status.", "error")
        return
    now = datetime.utcnow().isoformat()
    db.execute(f"UPDATE requests SET status=?, {field}=? WHERE id=?", (new_status, now, req_id))
    db.commit()

@app.route("/request/<int:req_id>/dispatch", methods=["POST"])
@login_required
def dispatch_request(req_id):
    advance_status(req_id, "matched", "dispatched", "dispatched_at", actor="matched")
    return redirect(url_for("request_detail", req_id=req_id, st=g.token))

@app.route("/request/<int:req_id>/transit", methods=["POST"])
@login_required
def transit_request(req_id):
    advance_status(req_id, "dispatched", "in_transit", "transit_at", actor="matched")
    return redirect(url_for("request_detail", req_id=req_id, st=g.token))

@app.route("/request/<int:req_id>/fulfill", methods=["POST"])
@login_required
def fulfill_request(req_id):
    # Delivery is confirmed by the REQUESTING hospital (the one receiving
    # the asset) since they're the ones who actually know it has arrived.
    advance_status(req_id, "in_transit", "delivered", "delivered_at", actor="requester")
    return redirect(url_for("request_detail", req_id=req_id, st=g.token))

@app.route("/network")
@login_required
def network():
    db = get_db()
    hospitals = db.execute("SELECT id, name, lat, lon FROM hospitals").fetchall()
    return render_template("network.html", hospitals=hospitals)

@app.route("/api/network")
@login_required
def api_network():
    db = get_db()
    hospitals = db.execute("SELECT id, name, lat, lon FROM hospitals").fetchall()
    active = db.execute(
        "SELECT r.id, r.asset_type, r.quantity, r.status, r.tier, r.eta_min, r.distance_km, r.transit_at, r.dispatched_at, r.matched_at, "
        "r.route_geojson, r.traffic_level, r.traffic_multiplier, "
        "h1.lat AS r_lat, h1.lon AS r_lon, h2.lat AS m_lat, h2.lon AS m_lon, "
        "h1.name AS requester_name, h2.name AS matched_name "
        "FROM requests r JOIN hospitals h1 ON h1.id=r.requester_id "
        "JOIN hospitals h2 ON h2.id=r.matched_hospital_id "
        "WHERE r.status IN ('matched','dispatched','in_transit')"
    ).fetchall()

    active_out = []
    for a in active:
        d = dict(a)
        coords = json.loads(d.pop("route_geojson")) if d.get("route_geojson") else [[d["m_lat"], d["m_lon"]], [d["r_lat"], d["r_lon"]]]
        d["route"] = coords
        # keep traffic "live" - recompute at the route midpoint on every poll
        mid = coords[len(coords)//2]
        level, mult, _src = live_traffic_multiplier(mid[0], mid[1], seed_key=f"req{d['id']}")
        d["traffic_level"] = level
        d["traffic_multiplier"] = mult
        active_out.append(d)

    return jsonify({
        "hospitals": [dict(h) for h in hospitals],
        "active": active_out,
        "server_now": datetime.utcnow().isoformat(),
    })

# ---------------------------------------------------------------- doctors (static directory)
@app.route("/doctors")
@login_required
def doctors():
    return render_template("doctors.html", doctors=DOCTORS)

# ---------------------------------------------------------------- analytics
@app.route("/analytics")
@login_required
def analytics():
    db = get_db()
    me = current_hospital()

    created = datetime.fromisoformat(me["created_at"])
    delta = datetime.utcnow() - created
    uptime_str = f"{delta.days}d {delta.seconds // 3600}h"

    inv_total = db.execute("SELECT COUNT(*) c FROM inventory WHERE hospital_id=?", (me["id"],)).fetchone()["c"]
    zero_rows = db.execute(
        "SELECT asset_type FROM inventory WHERE hospital_id=? AND quantity=0 ORDER BY asset_type", (me["id"],)
    ).fetchall()
    zero_count = len(zero_rows)
    zero_ratio = (zero_count / inv_total) if inv_total else 0

    sent = db.execute("SELECT status, created_at FROM requests WHERE requester_id=?", (me["id"],)).fetchall()
    fulfilled_by_me = db.execute("SELECT status, created_at FROM requests WHERE matched_hospital_id=?", (me["id"],)).fetchall()

    sent_total = len(sent)
    unmatched_total = len([s for s in sent if s["status"] == "unmatched"])
    unmatched_ratio = (unmatched_total / sent_total) if sent_total else 0

    fulfill_total = len(fulfilled_by_me)
    fulfill_delivered = len([f for f in fulfilled_by_me if f["status"] == "delivered"])
    fulfill_rate = (fulfill_delivered / fulfill_total) if fulfill_total else 1.0

    score = 100 - (zero_ratio * 40) - (unmatched_ratio * 30) + ((fulfill_rate - 0.5) * 20)
    score = max(0, min(100, round(score)))

    weekday_counts = [0] * 7
    for e in list(sent) + list(fulfilled_by_me):
        try:
            weekday_counts[datetime.fromisoformat(e["created_at"]).weekday()] += 1
        except Exception:
            pass
    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    max_day = max(weekday_counts) if max(weekday_counts) > 0 else 1

    return render_template("analytics.html", me=me, uptime_str=uptime_str,
                            zero_count=zero_count, zero_rows=zero_rows, inv_total=inv_total,
                            score=score, sent_total=sent_total, fulfill_total=fulfill_total,
                            fulfill_delivered=fulfill_delivered,
                            weekday_labels=weekday_labels, weekday_counts=weekday_counts, max_day=max_day)

@app.route("/export/inventory.csv")
@login_required
def export_inventory_csv():
    import io, csv
    db = get_db()
    me = current_hospital()
    rows = db.execute(
        "SELECT category, asset_type, quantity, updated_at FROM inventory WHERE hospital_id=? ORDER BY category, asset_type",
        (me["id"],)
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Category", "Asset", "Quantity", "Last Updated (UTC)"])
    for r in rows:
        writer.writerow([r["category"], r["asset_type"], r["quantity"], r["updated_at"]])
    filename = f"{me['name'].replace(' ', '_')}_inventory.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "LifeRoute"})

@app.route("/sos/call", methods=["POST"])
@login_required
def sos_call():
    me = current_hospital()
    phone = request.form.get("phone", "").strip()
    if not phone:
        return jsonify({"ok": False, "message": "No phone number provided."}), 400

    if not (TWILIO_AVAILABLE and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        return jsonify({
            "ok": False,
            "demo": True,
            "message": "Twilio isn't configured on this server, so no real call was placed. "
                       "See README.md \u2192 'Real SOS calling' to connect a Twilio account."
        })

    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        twiml = (
            f'<Response><Say voice="alice">'
            f'This is an automated emergency broadcast from {me["name"]}, on the LifeRoute medical '
            f'network. Please respond as soon as possible. Repeating. '
            f'This is an automated emergency broadcast from {me["name"]}.'
            f'</Say></Response>'
        )
        call = client.calls.create(twiml=twiml, to=phone, from_=TWILIO_FROM_NUMBER)
        return jsonify({"ok": True, "message": f"Call placed to {phone}.", "call_sid": call.sid})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Call failed: {e}"}), 500

@app.route("/favicon.ico")
def favicon():
    return ("", 204)

import os

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
