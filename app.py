"""
LuckyCart — Lucky Draw E-Commerce Platform
Flask + PostgreSQL | Railway-ready

FIX: init_db() now runs at module load time so gunicorn workers
     always initialise the schema before handling any request.
"""

import os, hashlib, hmac as hmac_mod, secrets, logging
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2, psycopg2.extras
from flask import Flask, render_template, request, jsonify, session, abort
from werkzeug.security import generate_password_hash, check_password_hash

# ── setup ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("luckycart")

DB_URL          = os.environ.get("DATABASE_URL", "postgresql://localhost/luckycart")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_SECRET = os.environ.get("RAZORPAY_SECRET", "")
ADMIN_EMAIL     = os.environ.get("ADMIN_EMAIL", "admin@luckycart.in")

rz_client = None
if RAZORPAY_KEY_ID and RAZORPAY_SECRET:
    try:
        import razorpay
        rz_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))
        log.info("Razorpay client initialised.")
    except ImportError:
        log.warning("razorpay not installed; demo mode active.")

# ── db ────────────────────────────────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn

def query(sql, params=(), one=False, commit=False):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        if commit:
            conn.commit()
            return cur.rowcount
        result = cur.fetchone() if one else cur.fetchall()
        return result
    finally:
        conn.close()

# ── schema ────────────────────────────────────────────────────────────────────
SCHEMA_STMTS = [
    """CREATE TABLE IF NOT EXISTS users (
        id         SERIAL PRIMARY KEY,
        name       TEXT NOT NULL,
        email      TEXT UNIQUE NOT NULL,
        phone      TEXT,
        password   TEXT NOT NULL,
        is_admin   BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS products (
        id             SERIAL PRIMARY KEY,
        name           TEXT NOT NULL,
        description    TEXT,
        category       TEXT NOT NULL,
        emoji          TEXT DEFAULT '📦',
        image_url      TEXT,
        original_price INTEGER NOT NULL,
        ticket_price   INTEGER NOT NULL,
        total_slots    INTEGER NOT NULL,
        filled_slots   INTEGER DEFAULT 0,
        status         TEXT DEFAULT 'active',
        draw_seed      TEXT,
        winner_user_id INTEGER,
        created_at     TIMESTAMPTZ DEFAULT NOW(),
        drawn_at       TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS tickets (
        id                  SERIAL PRIMARY KEY,
        product_id          INTEGER NOT NULL,
        user_id             INTEGER NOT NULL,
        quantity            INTEGER NOT NULL DEFAULT 1,
        amount_paid         INTEGER NOT NULL,
        razorpay_order_id   TEXT,
        razorpay_payment_id TEXT,
        payment_status      TEXT DEFAULT 'pending',
        is_winner           BOOLEAN DEFAULT FALSE,
        created_at          TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS draws (
        id                 SERIAL PRIMARY KEY,
        product_id         INTEGER NOT NULL,
        winner_ticket_id   INTEGER,
        winner_user_id     INTEGER,
        seed               TEXT NOT NULL,
        winning_slot       INTEGER NOT NULL,
        total_slots        INTEGER NOT NULL,
        refund_amount      INTEGER NOT NULL,
        refund_status      TEXT DEFAULT 'pending',
        razorpay_refund_id TEXT,
        drawn_at           TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tix_prod ON tickets(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_tix_user ON tickets(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_prod_status ON products(status)",
]

SEED_PRODUCTS = [
    ("iPhone 15 Pro", "256GB Natural Titanium · A17 Pro chip · USB-C", "Electronics", "📱", 134900, 3499, 100),
    ("Sony WH-1000XM5", "Industry-leading ANC · 30h battery · LDAC", "Electronics", "🎧", 29990, 799, 60),
    ("Nike Air Max 270", "Max Air unit · breathable mesh · iconic silhouette", "Fashion", "👟", 13995, 349, 50),
    ("PS5 Console", "PlayStation 5 · DualSense · 825GB SSD · 4K", "Gaming", "🎮", 54990, 1299, 80),
    ("Dyson V15 Detect", "Laser dust detection · 60-min runtime · HEPA", "Lifestyle", "🌀", 52900, 1199, 40),
    ("MacBook Air M3", "15-inch · 8GB RAM · 256GB SSD · 18h battery", "Electronics", "💻", 134900, 2999, 120),
    ("JBL Flip 6", "IP67 waterproof · 12h playtime · PartyBoost", "Electronics", "🔊", 9999, 249, 55),
    ("Adidas Ultraboost 23", "Continental™ rubber · Boost midsole · Primeknit", "Sports", "🏃", 16999, 429, 60),
    ("Kindle Paperwhite", "6.8\" 300ppi display · warm light · 10 weeks battery", "Lifestyle", "📚", 14999, 349, 40),
    ("Xbox Series X", "4K 120fps · 1TB SSD · Quick Resume · Game Pass", "Gaming", "🕹️", 49990, 1199, 75),
    ("Fossil Gen 6 Watch", "Wear OS · SpO2 · GPS · 24h battery", "Fashion", "⌚", 22995, 549, 45),
    ("Ray-Ban Wayfarer", "Classic polarised · UV400 · acetate frame", "Fashion", "🕶️", 12500, 299, 50),
]

_db_initialised = False

def init_db():
    global _db_initialised
    if _db_initialised:
        return
    try:
        conn = get_db()
        cur  = conn.cursor()
        for stmt in SCHEMA_STMTS:
            cur.execute(stmt)
        # Seed products if empty
        cur.execute("SELECT COUNT(*) AS c FROM products")
        if cur.fetchone()["c"] == 0:
            for name, desc, cat, emoji, orig, ticket, slots in SEED_PRODUCTS:
                cur.execute(
                    "INSERT INTO products (name,description,category,emoji,original_price,ticket_price,total_slots) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (name, desc, cat, emoji, orig * 100, ticket * 100, slots),
                )
            log.info("Seeded %d demo products.", len(SEED_PRODUCTS))
        conn.commit()
        conn.close()
        _db_initialised = True
        log.info("Database ready.")
    except Exception as e:
        log.error("DB init failed: %s", e)
        raise

# ── CRITICAL: run at import time so every gunicorn worker initialises DB ──────
try:
    init_db()
except Exception:
    pass  # will retry on first request via before_request

@app.before_request
def ensure_db():
    if not _db_initialised:
        init_db()

# ── auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if "user_id" not in session:
            return jsonify({"error": "Login required"}), 401
        return f(*a, **kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("is_admin"):
            abort(403)
        return f(*a, **kw)
    return w

def current_user():
    if "user_id" not in session:
        return None
    return query("SELECT * FROM users WHERE id=%s", (session["user_id"],), one=True)

# ── draw engine ───────────────────────────────────────────────────────────────
def run_draw(product_id: int):
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM products WHERE id=%s FOR UPDATE", (product_id,))
        product = cur.fetchone()
        if not product or product["status"] != "active":
            return {"error": "Product not available for draw"}

        cur.execute(
            "SELECT t.*, u.name AS uname, u.email AS uemail FROM tickets t "
            "JOIN users u ON u.id=t.user_id "
            "WHERE t.product_id=%s AND t.payment_status='paid' ORDER BY t.id",
            (product_id,),
        )
        tickets = cur.fetchall()

        slots = []
        for t in tickets:
            for _ in range(t["quantity"]):
                slots.append(t)

        if not slots:
            return {"error": "No paid tickets"}

        seed        = secrets.token_hex(32)
        hash_input  = f"{seed}:{product_id}:{len(slots)}".encode()
        hash_val    = hashlib.sha256(hash_input).hexdigest()
        winning_slot = int(hash_val, 16) % len(slots)
        winner      = slots[winning_slot]

        cur.execute(
            "SELECT COALESCE(SUM(amount_paid),0) AS tot FROM tickets "
            "WHERE product_id=%s AND user_id=%s AND payment_status='paid'",
            (product_id, winner["user_id"]),
        )
        refund_amount = cur.fetchone()["tot"]

        cur.execute(
            "INSERT INTO draws (product_id,winner_ticket_id,winner_user_id,seed,winning_slot,total_slots,refund_amount) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (product_id, winner["id"], winner["user_id"], seed, winning_slot, len(slots), refund_amount),
        )
        draw_id = cur.fetchone()["id"]

        cur.execute(
            "UPDATE products SET status='drawn',draw_seed=%s,winner_user_id=%s,drawn_at=NOW() WHERE id=%s",
            (seed, winner["user_id"], product_id),
        )
        cur.execute("UPDATE tickets SET is_winner=TRUE WHERE id=%s", (winner["id"],))
        conn.commit()

        # Razorpay refund
        refund_id = None
        if rz_client and winner.get("razorpay_payment_id"):
            try:
                r = rz_client.payment.refund(winner["razorpay_payment_id"], {"amount": refund_amount})
                refund_id = r["id"]
                cur.execute(
                    "UPDATE draws SET refund_status='processed',razorpay_refund_id=%s WHERE id=%s",
                    (refund_id, draw_id),
                )
                cur.execute(
                    "UPDATE tickets SET payment_status='refunded' WHERE product_id=%s AND user_id=%s",
                    (product_id, winner["user_id"]),
                )
                conn.commit()
            except Exception as e:
                log.error("Refund failed: %s", e)

        log.info("Draw done: product=%s winner=%s slot=%s/%s", product_id, winner["user_id"], winning_slot, len(slots))
        return {
            "draw_id": draw_id, "winner_name": winner["uname"],
            "winner_email": winner["uemail"], "winning_slot": winning_slot,
            "total_slots": len(slots), "seed": seed,
            "refund_amount": refund_amount, "refund_id": refund_id,
        }
    except Exception as e:
        conn.rollback()
        log.error("Draw error: %s", e)
        return {"error": str(e)}
    finally:
        conn.close()

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.json or {}
    name  = d.get("name","").strip()
    email = d.get("email","").strip().lower()
    phone = d.get("phone","").strip()
    pwd   = d.get("password","")
    if not name or not email or not pwd:
        return jsonify({"error": "Name, email and password required"}), 400
    if len(pwd) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if query("SELECT id FROM users WHERE email=%s", (email,), one=True):
        return jsonify({"error": "Email already registered"}), 409
    is_admin = email == ADMIN_EMAIL
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (name,email,phone,password,is_admin) VALUES (%s,%s,%s,%s,%s) RETURNING id,is_admin",
        (name, email, phone, generate_password_hash(pwd), is_admin),
    )
    row = cur.fetchone(); conn.commit(); conn.close()
    session["user_id"] = row["id"]; session["is_admin"] = row["is_admin"]
    return jsonify({"message": "Registered!", "user_id": row["id"], "is_admin": row["is_admin"]}), 201

@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.json or {}
    email = d.get("email","").strip().lower()
    pwd   = d.get("password","")
    user  = query("SELECT * FROM users WHERE email=%s", (email,), one=True)
    if not user or not check_password_hash(user["password"], pwd):
        return jsonify({"error": "Invalid credentials"}), 401
    session["user_id"] = user["id"]; session["is_admin"] = user["is_admin"]
    return jsonify({"message": "Logged in", "name": user["name"], "is_admin": user["is_admin"]})

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})

@app.route("/api/auth/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "id": u["id"], "name": u["name"], "email": u["email"], "is_admin": u["is_admin"]})

# ═══════════════════════════════════════════════════════════════════════════════
#  PRODUCTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/products")
def list_products():
    cat    = request.args.get("category")
    status = request.args.get("status", "active")
    sql, p = "SELECT * FROM products WHERE status=%s", [status]
    if cat:
        sql += " AND category=%s"; p.append(cat)
    sql += " ORDER BY created_at DESC"
    rows = query(sql, p)
    out = []
    for r in rows:
        item = dict(r)
        item["original_price_inr"] = r["original_price"] / 100
        item["ticket_price_inr"]   = r["ticket_price"] / 100
        item["slots_left"]         = r["total_slots"] - r["filled_slots"]
        item["fill_pct"]           = round(r["filled_slots"] / r["total_slots"] * 100, 1) if r["total_slots"] else 0
        item["discount_pct"]       = round((1 - r["ticket_price"] / r["original_price"]) * 100)
        out.append(item)
    return jsonify(out)

@app.route("/api/products/<int:pid>")
def get_product(pid):
    p = query("SELECT * FROM products WHERE id=%s", (pid,), one=True)
    if not p:
        return jsonify({"error": "Not found"}), 404
    recent = query(
        "SELECT u.name, t.quantity, t.created_at FROM tickets t "
        "JOIN users u ON u.id=t.user_id "
        "WHERE t.product_id=%s AND t.payment_status='paid' ORDER BY t.created_at DESC LIMIT 5",
        (pid,),
    )
    r = dict(p)
    r["ticket_price_inr"]   = p["ticket_price"] / 100
    r["original_price_inr"] = p["original_price"] / 100
    r["slots_left"]         = p["total_slots"] - p["filled_slots"]
    r["recent_buyers"]      = [dict(x) for x in recent]
    return jsonify(r)

@app.route("/api/admin/products", methods=["POST"])
@login_required
@admin_required
def create_product():
    d = request.json or {}
    for f in ["name","category","original_price","ticket_price","total_slots"]:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO products (name,description,category,emoji,image_url,original_price,ticket_price,total_slots) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (d["name"], d.get("description",""), d["category"], d.get("emoji","📦"),
         d.get("image_url"), int(d["original_price"]*100), int(d["ticket_price"]*100), d["total_slots"]),
    )
    pid = cur.fetchone()["id"]; conn.commit(); conn.close()
    return jsonify({"message": "Created", "product_id": pid}), 201

@app.route("/api/admin/products/<int:pid>", methods=["PATCH"])
@login_required
@admin_required
def update_product(pid):
    d = request.json or {}
    allowed = ["name","description","category","emoji","image_url","status"]
    sets, vals = [], []
    for k in allowed:
        if k in d:
            sets.append(f"{k}=%s"); vals.append(d[k])
    if not sets:
        return jsonify({"error": "Nothing to update"}), 400
    vals.append(pid)
    query(f"UPDATE products SET {', '.join(sets)} WHERE id=%s", vals, commit=True)
    return jsonify({"message": "Updated"})

# ═══════════════════════════════════════════════════════════════════════════════
#  PAYMENTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/orders/create", methods=["POST"])
@login_required
def create_order():
    d          = request.json or {}
    product_id = d.get("product_id")
    quantity   = max(1, min(10, int(d.get("quantity", 1))))
    product    = query("SELECT * FROM products WHERE id=%s AND status='active'", (product_id,), one=True)
    if not product:
        return jsonify({"error": "Product not available"}), 404
    slots_left = product["total_slots"] - product["filled_slots"]
    if quantity > slots_left:
        return jsonify({"error": f"Only {slots_left} slot(s) left"}), 400
    amount_paise = product["ticket_price"] * quantity
    if rz_client:
        rz_order    = rz_client.order.create({"amount": amount_paise, "currency": "INR"})
        rz_order_id = rz_order["id"]
    else:
        rz_order_id = f"demo_order_{secrets.token_hex(6)}"
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO tickets (product_id,user_id,quantity,amount_paid,razorpay_order_id) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (product_id, session["user_id"], quantity, amount_paise, rz_order_id),
    )
    ticket_id = cur.fetchone()["id"]; conn.commit(); conn.close()
    return jsonify({
        "order_id": rz_order_id, "amount": amount_paise, "currency": "INR",
        "key_id": RAZORPAY_KEY_ID, "ticket_id": ticket_id, "product_name": product["name"],
    })

@app.route("/api/orders/verify", methods=["POST"])
@login_required
def verify_payment():
    d = request.json or {}
    rz_order_id   = d.get("razorpay_order_id","")
    rz_payment_id = d.get("razorpay_payment_id","")
    rz_sig        = d.get("razorpay_signature","")
    ticket_id     = d.get("ticket_id")
    if rz_client and rz_sig:
        body     = f"{rz_order_id}|{rz_payment_id}"
        expected = hmac_mod.new(RAZORPAY_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac_mod.compare_digest(expected, rz_sig):
            return jsonify({"error": "Invalid signature"}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=%s AND user_id=%s", (ticket_id, session["user_id"]))
    ticket = cur.fetchone()
    if not ticket:
        conn.close(); return jsonify({"error": "Ticket not found"}), 404
    pay_id = rz_payment_id or f"demo_pay_{secrets.token_hex(6)}"
    cur.execute("UPDATE tickets SET payment_status='paid',razorpay_payment_id=%s WHERE id=%s", (pay_id, ticket_id))
    cur.execute(
        "UPDATE products SET filled_slots=filled_slots+%s WHERE id=%s RETURNING filled_slots,total_slots,id",
        (ticket["quantity"], ticket["product_id"]),
    )
    prod = cur.fetchone(); conn.commit(); conn.close()
    draw_result = None
    if prod["filled_slots"] >= prod["total_slots"]:
        draw_result = run_draw(prod["id"])
    return jsonify({"message": "You're in the draw! 🎉", "filled_slots": prod["filled_slots"],
                    "total_slots": prod["total_slots"], "draw_result": draw_result})

@app.route("/api/orders/demo-pay", methods=["POST"])
@login_required
def demo_pay():
    d         = request.json or {}
    ticket_id = d.get("ticket_id")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=%s AND user_id=%s", (ticket_id, session["user_id"]))
    ticket = cur.fetchone()
    if not ticket:
        conn.close(); return jsonify({"error": "Ticket not found"}), 404
    cur.execute("UPDATE tickets SET payment_status='paid',razorpay_payment_id=%s WHERE id=%s",
                (f"pay_demo_{secrets.token_hex(8)}", ticket_id))
    cur.execute(
        "UPDATE products SET filled_slots=filled_slots+%s WHERE id=%s RETURNING filled_slots,total_slots,id",
        (ticket["quantity"], ticket["product_id"]),
    )
    prod = cur.fetchone(); conn.commit(); conn.close()
    draw_result = None
    if prod["filled_slots"] >= prod["total_slots"]:
        draw_result = run_draw(prod["id"])
    return jsonify({"message": "Demo payment done!", "filled_slots": prod["filled_slots"],
                    "total_slots": prod["total_slots"], "draw_result": draw_result})

# ═══════════════════════════════════════════════════════════════════════════════
#  USER
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/my/tickets")
@login_required
def my_tickets():
    rows = query(
        "SELECT t.*,p.name AS pname,p.emoji,p.category,p.original_price,p.status AS pstatus,p.draw_seed "
        "FROM tickets t JOIN products p ON p.id=t.product_id "
        "WHERE t.user_id=%s ORDER BY t.created_at DESC",
        (session["user_id"],),
    )
    return jsonify([dict(r) for r in rows])

@app.route("/api/my/wins")
@login_required
def my_wins():
    rows = query(
        "SELECT d.*,p.name AS pname,p.emoji,p.original_price FROM draws d "
        "JOIN products p ON p.id=d.product_id WHERE d.winner_user_id=%s ORDER BY d.drawn_at DESC",
        (session["user_id"],),
    )
    return jsonify([dict(r) for r in rows])

# ═══════════════════════════════════════════════════════════════════════════════
#  DRAWS (public)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/draws")
def draw_history():
    rows = query(
        "SELECT d.id,d.drawn_at,d.winning_slot,d.total_slots,d.seed,d.refund_amount,d.refund_status,"
        "p.name AS pname,p.emoji,u.name AS wname FROM draws d "
        "JOIN products p ON p.id=d.product_id JOIN users u ON u.id=d.winner_user_id "
        "ORDER BY d.drawn_at DESC LIMIT 50"
    )
    return jsonify([dict(r) for r in rows])

@app.route("/api/draws/<int:did>/verify")
def verify_draw(did):
    draw = query("SELECT * FROM draws WHERE id=%s", (did,), one=True)
    if not draw:
        return jsonify({"error": "Not found"}), 404
    hi = f"{draw['seed']}:{draw['product_id']}:{draw['total_slots']}".encode()
    hv = hashlib.sha256(hi).hexdigest()
    cs = int(hv, 16) % draw["total_slots"]
    return jsonify({"draw_id": did, "seed": draw["seed"], "hash": hv,
                    "computed_slot": cs, "recorded_slot": draw["winning_slot"],
                    "is_valid": cs == draw["winning_slot"]})

# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/stats")
@login_required
@admin_required
def admin_stats():
    return jsonify({
        "total_users":     query("SELECT COUNT(*) AS c FROM users", one=True)["c"],
        "active_draws":    query("SELECT COUNT(*) AS c FROM products WHERE status='active'", one=True)["c"],
        "completed_draws": query("SELECT COUNT(*) AS c FROM draws", one=True)["c"],
        "total_revenue":   query("SELECT COALESCE(SUM(amount_paid),0) AS c FROM tickets WHERE payment_status='paid'", one=True)["c"],
        "total_refunds":   query("SELECT COALESCE(SUM(refund_amount),0) AS c FROM draws WHERE refund_status='processed'", one=True)["c"],
    })

@app.route("/api/admin/tickets")
@login_required
@admin_required
def admin_all_tickets():
    rows = query(
        "SELECT t.*,u.name AS uname,u.email,p.name AS pname FROM tickets t "
        "JOIN users u ON u.id=t.user_id JOIN products p ON p.id=t.product_id "
        "ORDER BY t.created_at DESC LIMIT 200"
    )
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/draw/<int:pid>", methods=["POST"])
@login_required
@admin_required
def manual_draw(pid):
    return jsonify(run_draw(pid))

# ═══════════════════════════════════════════════════════════════════════════════
#  PAGES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html", razorpay_key=RAZORPAY_KEY_ID, user=current_user())

@app.route("/admin")
@login_required
@admin_required
def admin_page():
    return render_template("admin.html", user=current_user())

@app.route("/my-tickets")
@login_required
def my_tickets_page():
    return render_template("my_tickets.html", user=current_user())

# ── health check ──────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "db": _db_initialised})

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
