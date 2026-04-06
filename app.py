"""
LuckyCart — Lucky Draw E-Commerce Platform
Flask + PostgreSQL | Railway-ready

Business model:
  - Products listed with a ticket_price (far below original_price)
  - Buyers pay ticket_price → enter the draw
  - When all slots fill → lucky draw runs
  - ONE winner gets the actual PRODUCT shipped to them (not a refund)
  - Everyone else just paid a small ticket entry fee
"""

import os, hashlib, secrets, logging
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2, psycopg2.extras
from flask import Flask, render_template, request, jsonify, session, abort
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("luckycart")

DB_URL      = os.environ.get("DATABASE_URL", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@luckycart.in")
UPI_ID      = os.environ.get("UPI_ID",   "yourupi@ybl")
UPI_NAME    = os.environ.get("UPI_NAME", "LuckyCart")
UPI_QR_URL  = os.environ.get("UPI_QR_URL", "")

# ── global error handlers → always return JSON, never HTML ───────────────────
@app.errorhandler(400)
def e400(e): return jsonify({"error": str(e)}), 400
@app.errorhandler(401)
def e401(e): return jsonify({"error": "Unauthorised"}), 401
@app.errorhandler(403)
def e403(e): return jsonify({"error": "Forbidden"}), 403
@app.errorhandler(404)
def e404(e): return jsonify({"error": "Not found"}), 404
@app.errorhandler(500)
def e500(e): return jsonify({"error": "Server error", "detail": str(e)}), 500
@app.errorhandler(Exception)
def eany(e):
    log.exception("Unhandled: %s", e)
    return jsonify({"error": "Server error", "detail": str(e)}), 500

# ── db ────────────────────────────────────────────────────────────────────────
def get_db():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not set. Add PostgreSQL plugin in Railway.")
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
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL, phone TEXT, address TEXT,
        password TEXT NOT NULL, is_admin BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL, description TEXT,
        category TEXT NOT NULL, emoji TEXT DEFAULT '📦', image_url TEXT,
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
        id             SERIAL PRIMARY KEY,
        product_id     INTEGER NOT NULL,
        user_id        INTEGER NOT NULL,
        quantity       INTEGER NOT NULL DEFAULT 1,
        amount_paid    INTEGER NOT NULL,
        order_ref      TEXT,
        utr            TEXT,
        payment_status TEXT DEFAULT 'pending',
        is_winner      BOOLEAN DEFAULT FALSE,
        created_at     TIMESTAMPTZ DEFAULT NOW(),
        confirmed_at   TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS draws (
        id               SERIAL PRIMARY KEY,
        product_id       INTEGER NOT NULL,
        winner_ticket_id INTEGER,
        winner_user_id   INTEGER,
        seed             TEXT NOT NULL,
        winning_slot     INTEGER NOT NULL,
        total_slots      INTEGER NOT NULL,
        prize_value      INTEGER NOT NULL,
        delivery_status  TEXT DEFAULT 'pending',
        delivery_address TEXT,
        tracking_info    TEXT,
        drawn_at         TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tix_prod    ON tickets(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_tix_user    ON tickets(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tix_status  ON tickets(payment_status)",
    "CREATE INDEX IF NOT EXISTS idx_prod_status ON products(status)",
]

SEED = [
    ("iPhone 15 Pro",    "256GB Natural Titanium · A17 Pro · USB-C",      "Electronics","📱",134900,3499,100),
    ("Sony WH-1000XM5", "Industry-leading ANC · 30h battery · LDAC",      "Electronics","🎧",29990,799,60),
    ("Nike Air Max 270", "Max Air unit · breathable mesh · iconic look",   "Fashion",    "👟",13995,349,50),
    ("PS5 Console",      "DualSense · 825GB SSD · 4K 120fps",             "Gaming",     "🎮",54990,1299,80),
    ("Dyson V15 Detect", "Laser dust detection · 60-min · HEPA",          "Lifestyle",  "🌀",52900,1199,40),
    ("MacBook Air M3",   "15-inch · 8GB · 256GB SSD · 18h battery",       "Electronics","💻",134900,2999,120),
    ("JBL Flip 6",       "IP67 waterproof · 12h playtime · PartyBoost",   "Electronics","🔊",9999,249,55),
    ("Adidas Ultraboost","Continental™ rubber · Boost · Primeknit upper", "Sports",     "🏃",16999,429,60),
    ("Kindle Paperwhite","6.8\" 300ppi · warm light · 10 weeks battery",  "Lifestyle",  "📚",14999,349,40),
    ("Xbox Series X",    "4K 120fps · 1TB SSD · Quick Resume",            "Gaming",     "🕹️",49990,1199,75),
    ("Fossil Gen 6",     "Wear OS · SpO2 · GPS · 24h battery",            "Fashion",    "⌚",22995,549,45),
    ("Ray-Ban Wayfarer", "Classic polarised · UV400 · acetate frame",      "Fashion",    "🕶️",12500,299,50),
]

_db_ok = False

def init_db():
    global _db_ok
    if _db_ok:
        return
    conn = get_db()   # raises if DB_URL missing — caught by caller
    try:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt)
        cur.execute("SELECT COUNT(*) AS c FROM products")
        if cur.fetchone()["c"] == 0:
            for name,desc,cat,emoji,orig,ticket,slots in SEED:
                cur.execute(
                    "INSERT INTO products (name,description,category,emoji,original_price,ticket_price,total_slots) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (name,desc,cat,emoji,orig*100,ticket*100,slots)
                )
            log.info("Seeded %d products.", len(SEED))
        conn.commit()
        _db_ok = True
        log.info("Database ready.")
    except Exception as e:
        conn.rollback()
        log.error("Schema init error: %s", e)
        raise
    finally:
        conn.close()

# Try at import time — if it fails, before_request will retry
try:
    init_db()
except Exception as e:
    log.warning("DB not available at startup (%s) — will retry on first request.", e)

@app.before_request
def ensure_db():
    """Retry DB init on every request. Returns clean JSON 503 on failure (never raises)."""
    if _db_ok:
        return
    # Health check always succeeds so Railway marks deployment healthy
    if request.path == "/health":
        return
    try:
        init_db()
    except Exception as e:
        msg = str(e)
        if "DATABASE_URL" in msg:
            msg = "DATABASE_URL not set — add PostgreSQL plugin in Railway dashboard."
        log.warning("DB unavailable: %s", e)
        return jsonify({"error": "Database not ready", "detail": msg}), 503

# ── auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def w(*a,**kw):
        if "user_id" not in session:
            return jsonify({"error":"Login required"}),401
        return f(*a,**kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not session.get("is_admin"):
            return jsonify({"error":"Admin only"}),403
        return f(*a,**kw)
    return w

def current_user():
    if "user_id" not in session: return None
    try:
        return query("SELECT * FROM users WHERE id=%s",(session["user_id"],),one=True)
    except Exception:
        return None

def upi_link(amount_paise, ref):
    amt = amount_paise / 100
    return (f"upi://pay?pa={UPI_ID}&pn={UPI_NAME.replace(' ','%20')}"
            f"&am={amt:.2f}&cu=INR&tn=LuckyCart%20{ref}")

# ── draw engine ───────────────────────────────────────────────────────────────
def run_draw(product_id):
    """
    Pick one winner from paid tickets.
    Winner gets the PRODUCT shipped to them.
    Everyone else paid a small ticket fee — that's the deal.
    """
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM products WHERE id=%s FOR UPDATE",(product_id,))
        prod = cur.fetchone()
        if not prod or prod["status"] != "active":
            return {"error":"Product not available for draw"}

        cur.execute(
            "SELECT t.*,u.name AS uname,u.email AS uemail,u.phone AS uphone,u.address AS uaddress "
            "FROM tickets t JOIN users u ON u.id=t.user_id "
            "WHERE t.product_id=%s AND t.payment_status='paid' ORDER BY t.id",
            (product_id,)
        )
        tickets = cur.fetchall()
        # Expand: each ticket gives quantity slots
        slots = [t for t in tickets for _ in range(t["quantity"])]
        if not slots:
            return {"error":"No paid tickets to draw from"}

        # Verifiable randomness: SHA-256(seed:product_id:total_slots)
        seed         = secrets.token_hex(32)
        h            = hashlib.sha256(f"{seed}:{product_id}:{len(slots)}".encode()).hexdigest()
        winning_slot = int(h, 16) % len(slots)
        winner       = slots[winning_slot]

        # prize_value = original product price (what we're shipping)
        prize_value = prod["original_price"]

        cur.execute(
            "INSERT INTO draws (product_id,winner_ticket_id,winner_user_id,seed,winning_slot,total_slots,prize_value,delivery_address) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (product_id, winner["id"], winner["user_id"], seed,
             winning_slot, len(slots), prize_value,
             winner.get("uaddress") or "")
        )
        draw_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE products SET status='drawn',draw_seed=%s,winner_user_id=%s,drawn_at=NOW() WHERE id=%s",
            (seed, winner["user_id"], product_id)
        )
        cur.execute("UPDATE tickets SET is_winner=TRUE WHERE id=%s",(winner["id"],))
        conn.commit()

        log.info("🎉 Draw product=%s winner=%s (%s) prize=₹%.0f slot=%s/%s",
                 product_id, winner["uname"], winner["uemail"],
                 prize_value/100, winning_slot, len(slots))

        return {
            "draw_id":       draw_id,
            "winner_name":   winner["uname"],
            "winner_email":  winner["uemail"],
            "winner_phone":  winner.get("uphone",""),
            "winner_address":winner.get("uaddress",""),
            "winning_slot":  winning_slot,
            "total_slots":   len(slots),
            "seed":          seed,
            "prize_value":   prize_value,   # original product price in paise
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
    d=request.json or {}
    name=d.get("name","").strip(); email=d.get("email","").strip().lower()
    phone=d.get("phone","").strip(); pwd=d.get("password","")
    if not name or not email or not pwd:
        return jsonify({"error":"Name, email and password are required"}),400
    if len(pwd)<8:
        return jsonify({"error":"Password must be at least 8 characters"}),400
    if query("SELECT id FROM users WHERE email=%s",(email,),one=True):
        return jsonify({"error":"Email already registered"}),409
    conn=get_db(); cur=conn.cursor()
    cur.execute(
        "INSERT INTO users (name,email,phone,password,is_admin) VALUES (%s,%s,%s,%s,%s) RETURNING id,is_admin",
        (name,email,phone,generate_password_hash(pwd),email==ADMIN_EMAIL)
    )
    row=cur.fetchone(); conn.commit(); conn.close()
    session["user_id"]=row["id"]; session["is_admin"]=row["is_admin"]
    return jsonify({"message":"Registered!","user_id":row["id"],"is_admin":row["is_admin"]}),201

@app.route("/api/auth/login", methods=["POST"])
def login():
    d=request.json or {}
    user=query("SELECT * FROM users WHERE email=%s",(d.get("email","").strip().lower(),),one=True)
    if not user or not check_password_hash(user["password"],d.get("password","")):
        return jsonify({"error":"Invalid email or password"}),401
    session["user_id"]=user["id"]; session["is_admin"]=user["is_admin"]
    return jsonify({"message":"Logged in","name":user["name"],"is_admin":user["is_admin"]})

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear(); return jsonify({"message":"Logged out"})

@app.route("/api/auth/me")
def me():
    u=current_user()
    if not u: return jsonify({"logged_in":False})
    return jsonify({"logged_in":True,"id":u["id"],"name":u["name"],"email":u["email"],"is_admin":u["is_admin"]})

# ═══════════════════════════════════════════════════════════════════════════════
#  PRODUCTS
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/products")
def list_products():
    cat=request.args.get("category"); status=request.args.get("status","active")
    sql,p="SELECT * FROM products WHERE status=%s",[status]
    if cat: sql+=" AND category=%s"; p.append(cat)
    sql+=" ORDER BY created_at DESC"
    out=[]
    for r in query(sql,p):
        item=dict(r)
        item["original_price_inr"]=r["original_price"]/100
        item["ticket_price_inr"]  =r["ticket_price"]/100
        item["slots_left"]        =r["total_slots"]-r["filled_slots"]
        item["fill_pct"]          =round(r["filled_slots"]/r["total_slots"]*100,1) if r["total_slots"] else 0
        item["discount_pct"]      =round((1-r["ticket_price"]/r["original_price"])*100)
        out.append(item)
    return jsonify(out)

@app.route("/api/admin/products", methods=["POST"])
@login_required
@admin_required
def create_product():
    d=request.json or {}
    for f in ["name","category","original_price","ticket_price","total_slots"]:
        if f not in d: return jsonify({"error":f"Missing: {f}"}),400
    conn=get_db(); cur=conn.cursor()
    cur.execute(
        "INSERT INTO products (name,description,category,emoji,original_price,ticket_price,total_slots) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (d["name"],d.get("description",""),d["category"],d.get("emoji","📦"),
         int(d["original_price"]*100),int(d["ticket_price"]*100),d["total_slots"])
    )
    pid=cur.fetchone()["id"]; conn.commit(); conn.close()
    return jsonify({"message":"Draw created","product_id":pid}),201

@app.route("/api/admin/products/<int:pid>", methods=["PATCH"])
@login_required
@admin_required
def update_product(pid):
    d=request.json or {}; allowed=["name","description","category","emoji","status"]
    sets,vals=[],[]
    for k in allowed:
        if k in d: sets.append(f"{k}=%s"); vals.append(d[k])
    if not sets: return jsonify({"error":"Nothing to update"}),400
    vals.append(pid)
    query(f"UPDATE products SET {', '.join(sets)} WHERE id=%s",vals,commit=True)
    return jsonify({"message":"Updated"})

# ═══════════════════════════════════════════════════════════════════════════════
#  PAYMENTS — UPI / PhonePe
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/orders/create", methods=["POST"])
@login_required
def create_order():
    d=request.json or {}
    product_id=d.get("product_id"); quantity=max(1,min(10,int(d.get("quantity",1))))
    prod=query("SELECT * FROM products WHERE id=%s AND status='active'",(product_id,),one=True)
    if not prod: return jsonify({"error":"Product not available"}),404
    slots_left=prod["total_slots"]-prod["filled_slots"]
    if quantity>slots_left: return jsonify({"error":f"Only {slots_left} slot(s) left"}),400
    amount_paise=prod["ticket_price"]*quantity
    order_ref="LC"+secrets.token_hex(4).upper()
    conn=get_db(); cur=conn.cursor()
    cur.execute(
        "INSERT INTO tickets (product_id,user_id,quantity,amount_paid,order_ref,payment_status) VALUES (%s,%s,%s,%s,%s,'pending') RETURNING id",
        (product_id,session["user_id"],quantity,amount_paise,order_ref)
    )
    tid=cur.fetchone()["id"]; conn.commit(); conn.close()
    return jsonify({
        "ticket_id":   tid,
        "order_ref":   order_ref,
        "amount_paise":amount_paise,
        "amount_inr":  amount_paise/100,
        "upi_id":      UPI_ID,
        "upi_name":    UPI_NAME,
        "upi_link":    upi_link(amount_paise, order_ref),
        "upi_qr_url":  UPI_QR_URL,
        "product_name":prod["name"],
    })

@app.route("/api/orders/submit-utr", methods=["POST"])
@login_required
def submit_utr():
    d=request.json or {}
    tid=d.get("ticket_id"); utr=d.get("utr","").strip()
    if not utr or len(utr)<6:
        return jsonify({"error":"Please enter a valid UTR / transaction ID"}),400
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=%s AND user_id=%s",(tid,session["user_id"]))
    ticket=cur.fetchone()
    if not ticket: conn.close(); return jsonify({"error":"Ticket not found"}),404
    if ticket["payment_status"]!="pending": conn.close(); return jsonify({"error":"Already submitted"}),400
    cur.execute("UPDATE tickets SET utr=%s,payment_status='utr_submitted' WHERE id=%s",(utr,tid))
    conn.commit(); conn.close()
    return jsonify({"message":"Payment submitted! Ticket will be activated within 30 minutes.","ticket_id":tid})

@app.route("/api/admin/confirm-payment/<int:tid>", methods=["POST"])
@login_required
@admin_required
def confirm_payment(tid):
    conn=get_db(); cur=conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=%s",(tid,))
    ticket=cur.fetchone()
    if not ticket: conn.close(); return jsonify({"error":"Not found"}),404
    if ticket["payment_status"]=="paid": conn.close(); return jsonify({"error":"Already confirmed"}),400
    cur.execute("UPDATE tickets SET payment_status='paid',confirmed_at=NOW() WHERE id=%s",(tid,))
    cur.execute(
        "UPDATE products SET filled_slots=filled_slots+%s WHERE id=%s RETURNING filled_slots,total_slots,id",
        (ticket["quantity"],ticket["product_id"])
    )
    prod=cur.fetchone(); conn.commit(); conn.close()
    draw_result=None
    if prod["filled_slots"]>=prod["total_slots"]:
        draw_result=run_draw(prod["id"])
    return jsonify({"message":"Confirmed! Ticket activated.",
                    "filled_slots":prod["filled_slots"],"total_slots":prod["total_slots"],
                    "draw_result":draw_result})

@app.route("/api/admin/reject-payment/<int:tid>", methods=["POST"])
@login_required
@admin_required
def reject_payment(tid):
    query("UPDATE tickets SET payment_status='rejected' WHERE id=%s",(tid,),commit=True)
    return jsonify({"message":"Rejected."})

@app.route("/api/admin/mark-shipped/<int:draw_id>", methods=["POST"])
@login_required
@admin_required
def mark_shipped(draw_id):
    """Admin marks product as shipped to winner with tracking info."""
    d=request.json or {}
    tracking=d.get("tracking",""); address=d.get("address","")
    query(
        "UPDATE draws SET delivery_status='shipped',tracking_info=%s,delivery_address=%s WHERE id=%s",
        (tracking,address,draw_id),commit=True
    )
    return jsonify({"message":"Marked as shipped."})

@app.route("/api/admin/mark-delivered/<int:draw_id>", methods=["POST"])
@login_required
@admin_required
def mark_delivered(draw_id):
    query("UPDATE draws SET delivery_status='delivered' WHERE id=%s",(draw_id,),commit=True)
    return jsonify({"message":"Marked as delivered."})

# ═══════════════════════════════════════════════════════════════════════════════
#  USER
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/my/tickets")
@login_required
def my_tickets():
    rows=query(
        "SELECT t.*,p.name AS pname,p.emoji,p.category,p.original_price,p.status AS pstatus "
        "FROM tickets t JOIN products p ON p.id=t.product_id "
        "WHERE t.user_id=%s ORDER BY t.created_at DESC",
        (session["user_id"],)
    )
    return jsonify([dict(r) for r in rows])

@app.route("/api/my/wins")
@login_required
def my_wins():
    rows=query(
        "SELECT d.*,p.name AS pname,p.emoji,p.original_price,p.description "
        "FROM draws d JOIN products p ON p.id=d.product_id "
        "WHERE d.winner_user_id=%s ORDER BY d.drawn_at DESC",
        (session["user_id"],)
    )
    return jsonify([dict(r) for r in rows])

# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC draws
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/draws")
def draw_history():
    try:
        rows=query(
            "SELECT d.id,d.drawn_at,d.winning_slot,d.total_slots,d.seed,"
            "d.prize_value,d.delivery_status,"
            "p.name AS pname,p.emoji,u.name AS wname "
            "FROM draws d "
            "JOIN products p ON p.id=d.product_id "
            "JOIN users u    ON u.id=d.winner_user_id "
            "ORDER BY d.drawn_at DESC LIMIT 50"
        )
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])   # return empty list, not 500 — page still loads

@app.route("/api/draws/<int:did>/verify")
def verify_draw(did):
    draw=query("SELECT * FROM draws WHERE id=%s",(did,),one=True)
    if not draw: return jsonify({"error":"Not found"}),404
    h=hashlib.sha256(f"{draw['seed']}:{draw['product_id']}:{draw['total_slots']}".encode()).hexdigest()
    cs=int(h,16)%draw["total_slots"]
    return jsonify({"draw_id":did,"seed":draw["seed"],"hash":h,
                    "computed_slot":cs,"recorded_slot":draw["winning_slot"],
                    "is_valid":cs==draw["winning_slot"]})

# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════════════════════════════
@app.route("/api/admin/stats")
@login_required
@admin_required
def admin_stats():
    return jsonify({
        "total_users":      query("SELECT COUNT(*) AS c FROM users",one=True)["c"],
        "active_draws":     query("SELECT COUNT(*) AS c FROM products WHERE status='active'",one=True)["c"],
        "completed_draws":  query("SELECT COUNT(*) AS c FROM draws",one=True)["c"],
        "pending_payments": query("SELECT COUNT(*) AS c FROM tickets WHERE payment_status='utr_submitted'",one=True)["c"],
        "total_revenue":    query("SELECT COALESCE(SUM(amount_paid),0) AS c FROM tickets WHERE payment_status IN ('paid','utr_submitted')",one=True)["c"],
        "prizes_to_ship":   query("SELECT COUNT(*) AS c FROM draws WHERE delivery_status='pending'",one=True)["c"],
    })

@app.route("/api/admin/pending-payments")
@login_required
@admin_required
def pending_payments():
    rows=query(
        "SELECT t.*,u.name AS uname,u.email,u.phone,p.name AS pname,p.emoji "
        "FROM tickets t JOIN users u ON u.id=t.user_id JOIN products p ON p.id=t.product_id "
        "WHERE t.payment_status IN ('utr_submitted','pending') ORDER BY t.created_at DESC"
    )
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/pending-shipments")
@login_required
@admin_required
def pending_shipments():
    """Draws where product hasn't been shipped yet."""
    rows=query(
        "SELECT d.*,p.name AS pname,p.emoji,p.original_price,"
        "u.name AS wname,u.email AS wemail,u.phone AS wphone,u.address AS waddress "
        "FROM draws d "
        "JOIN products p ON p.id=d.product_id "
        "JOIN users u    ON u.id=d.winner_user_id "
        "WHERE d.delivery_status IN ('pending','shipped') "
        "ORDER BY d.drawn_at DESC"
    )
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/all-tickets")
@login_required
@admin_required
def admin_all_tickets():
    rows=query(
        "SELECT t.*,u.name AS uname,u.email,p.name AS pname,p.emoji "
        "FROM tickets t JOIN users u ON u.id=t.user_id JOIN products p ON p.id=t.product_id "
        "ORDER BY t.created_at DESC LIMIT 300"
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
    return render_template("index.html",upi_id=UPI_ID,upi_name=UPI_NAME,user=current_user())

@app.route("/admin")
@login_required
@admin_required
def admin_page():
    return render_template("admin.html",user=current_user())

@app.route("/my-tickets")
@login_required
def my_tickets_page():
    return render_template("my_tickets.html",user=current_user())

@app.route("/health")
def health():
    return jsonify({"status":"ok","db":_db_ok,"upi_configured":bool(UPI_ID and UPI_ID!="yourupi@ybl")}),200

if __name__=="__main__":
    init_db()
    app.run(debug=True,port=5000)
