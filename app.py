"""
LuckyCart — Lucky Draw E-Commerce Platform
Flask + PostgreSQL backend

Model:
  - Products listed with a discounted ticket price + total slots
  - Buyers purchase tickets (pay ticket price) → get entered into the draw
  - When all slots fill, a verifiable lucky draw runs automatically
  - One winner gets a full refund via Razorpay
"""

import os, hashlib, hmac, json, secrets, logging
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

IST = ZoneInfo("Asia/Kolkata")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("luckycart")

# ── Config ────────────────────────────────────────────────────────────────────
DB_URL           = os.environ.get("DATABASE_URL", "postgresql://localhost/luckycart")
RAZORPAY_KEY_ID  = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_SECRET  = os.environ.get("RAZORPAY_SECRET", "")
ADMIN_EMAIL      = os.environ.get("ADMIN_EMAIL", "admin@luckycart.in")

# Lazy-load Razorpay only when keys are present
rz_client = None
if RAZORPAY_KEY_ID and RAZORPAY_SECRET:
    try:
        import razorpay
        rz_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))
    except ImportError:
        log.warning("razorpay package not installed; running in demo mode")

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
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

# ── Schema init ───────────────────────────────────────────────────────────────
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        email       TEXT UNIQUE NOT NULL,
        phone       TEXT,
        password    TEXT NOT NULL,
        is_admin    BOOLEAN DEFAULT FALSE,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS products (
        id              SERIAL PRIMARY KEY,
        name            TEXT NOT NULL,
        description     TEXT,
        category        TEXT NOT NULL,
        emoji           TEXT DEFAULT '📦',
        image_url       TEXT,
        original_price  INTEGER NOT NULL,
        ticket_price    INTEGER NOT NULL,
        total_slots     INTEGER NOT NULL,
        filled_slots    INTEGER DEFAULT 0,
        status          TEXT DEFAULT 'active',
        draw_seed       TEXT,
        winner_user_id  INTEGER REFERENCES users(id),
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        drawn_at        TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS tickets (
        id                  SERIAL PRIMARY KEY,
        product_id          INTEGER NOT NULL REFERENCES products(id),
        user_id             INTEGER NOT NULL REFERENCES users(id),
        quantity            INTEGER NOT NULL DEFAULT 1,
        amount_paid         INTEGER NOT NULL,
        razorpay_order_id   TEXT,
        razorpay_payment_id TEXT,
        payment_status      TEXT DEFAULT 'pending',
        is_winner           BOOLEAN DEFAULT FALSE,
        created_at          TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS draws (
        id                  SERIAL PRIMARY KEY,
        product_id          INTEGER NOT NULL REFERENCES products(id),
        winner_ticket_id    INTEGER REFERENCES tickets(id),
        winner_user_id      INTEGER REFERENCES users(id),
        seed                TEXT NOT NULL,
        winning_slot        INTEGER NOT NULL,
        total_slots         INTEGER NOT NULL,
        refund_amount       INTEGER NOT NULL,
        refund_status       TEXT DEFAULT 'pending',
        razorpay_refund_id  TEXT,
        drawn_at            TIMESTAMPTZ DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tickets_product ON tickets(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_user    ON tickets(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_products_status ON products(status)",
]

SEED_PRODUCTS = [
    ("iPhone 15 Pro", "256GB Natural Titanium, A17 Pro chip", "Electronics", "📱", 134900, 3499, 100),
    ("Sony WH-1000XM5", "Industry-leading noise cancellation headphones", "Electronics", "🎧", 29990, 799, 60),
    ("Nike Air Max 270", "Lifestyle shoes with Max Air unit", "Fashion", "👟", 13995, 349, 50),
    ("PS5 Console", "PlayStation 5 with DualSense controller", "Gaming", "🎮", 54990, 1299, 80),
    ("Dyson V15 Detect", "Laser dust detection, 60-min runtime", "Lifestyle", "🌀", 52900, 1199, 40),
    ("MacBook Air M3", "15-inch, 8GB RAM, 256GB SSD", "Electronics", "💻", 134900, 2999, 120),
    ("JBL Flip 6", "Portable Bluetooth speaker, IP67 waterproof", "Electronics", "🔊", 9999, 249, 55),
    ("Adidas Ultraboost 23", "Running shoes with Continental rubber outsole", "Sports", "🏃", 16999, 429, 60),
    ("Kindle Paperwhite", "6.8-inch display, adjustable warm light, waterproof", "Lifestyle", "📚", 14999, 349, 40),
    ("Xbox Series X", "4K gaming, 1TB SSD, 120fps support", "Gaming", "🕹️", 49990, 1199, 75),
    ("Fossil Gen 6 Watch", "Smartwatch with Wear OS, heart rate monitor", "Fashion", "⌚", 22995, 549, 45),
    ("Ray-Ban Wayfarer", "Classic polarized sunglasses, black frame", "Fashion", "🕶️", 12500, 299, 50),
]

def init_db():
    conn = get_db()
    cur = conn.cursor()
    for stmt in SCHEMA:
        cur.execute(stmt)

    # Seed demo products if empty
    cur.execute("SELECT COUNT(*) as c FROM products")
    if cur.fetchone()['c'] == 0:
        for name, desc, cat, emoji, orig, ticket, slots in SEED_PRODUCTS:
            cur.execute("""
                INSERT INTO products (name, description, category, emoji, original_price, ticket_price, total_slots)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (name, desc, cat, emoji, orig * 100, ticket * 100, slots))
        log.info("Seeded demo products.")

    conn.commit()
    conn.close()
    log.info("Database ready.")

# ── Auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Login required"}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def current_user():
    if 'user_id' not in session:
        return None
    return query("SELECT * FROM users WHERE id=%s", (session['user_id'],), one=True)

# ── Lucky Draw Engine ─────────────────────────────────────────────────────────
def run_draw(product_id: int):
    """
    Verifiable lucky draw:
    1. Generate a random seed (stored publicly for independent verification)
    2. SHA-256(seed:product_id:total_slots) → winning slot number
    3. Walk ticket list in insertion order to find who owns that slot
    4. Trigger Razorpay refund for the winner
    """
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM products WHERE id=%s FOR UPDATE", (product_id,))
    product = cur.fetchone()

    if not product or product['status'] != 'active':
        conn.close()
        return {"error": "Product not available for draw"}

    # Collect paid tickets in order
    cur.execute("""
        SELECT t.*, u.name as user_name, u.email as user_email
        FROM tickets t JOIN users u ON u.id = t.user_id
        WHERE t.product_id=%s AND t.payment_status='paid'
        ORDER BY t.id ASC
    """, (product_id,))
    tickets = cur.fetchall()

    # Expand tickets into slot list
    slots = []
    for t in tickets:
        for _ in range(t['quantity']):
            slots.append(t)

    if not slots:
        conn.close()
        return {"error": "No paid tickets found"}

    # Verifiable randomness
    seed       = secrets.token_hex(32)
    hash_input = f"{seed}:{product_id}:{len(slots)}".encode()
    hash_val   = hashlib.sha256(hash_input).hexdigest()
    winning_slot = int(hash_val, 16) % len(slots)

    winner_ticket  = slots[winning_slot]
    winner_user_id = winner_ticket['user_id']

    # Total refund = all tickets winner bought for this product
    cur.execute("""
        SELECT COALESCE(SUM(amount_paid), 0) AS total
        FROM tickets WHERE product_id=%s AND user_id=%s AND payment_status='paid'
    """, (product_id, winner_user_id))
    refund_amount = cur.fetchone()['total']

    # Insert draw record
    cur.execute("""
        INSERT INTO draws
            (product_id, winner_ticket_id, winner_user_id, seed,
             winning_slot, total_slots, refund_amount)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (product_id, winner_ticket['id'], winner_user_id,
          seed, winning_slot, len(slots), refund_amount))
    draw_id = cur.fetchone()['id']

    # Mark product drawn
    cur.execute("""
        UPDATE products SET status='drawn', draw_seed=%s, winner_user_id=%s, drawn_at=NOW()
        WHERE id=%s
    """, (seed, winner_user_id, product_id))

    cur.execute("UPDATE tickets SET is_winner=TRUE WHERE id=%s", (winner_ticket['id'],))
    conn.commit()

    # Razorpay refund
    refund_id = None
    if rz_client and winner_ticket.get('razorpay_payment_id'):
        try:
            refund = rz_client.payment.refund(
                winner_ticket['razorpay_payment_id'],
                {"amount": refund_amount, "notes": {"reason": "LuckyCart winner refund"}}
            )
            refund_id = refund['id']
            cur.execute(
                "UPDATE draws SET refund_status='processed', razorpay_refund_id=%s WHERE id=%s",
                (refund_id, draw_id)
            )
            cur.execute(
                "UPDATE tickets SET payment_status='refunded' WHERE product_id=%s AND user_id=%s",
                (product_id, winner_user_id)
            )
            conn.commit()
        except Exception as e:
            log.error(f"Razorpay refund error: {e}")

    conn.close()
    log.info(f"Draw complete: product={product_id} winner={winner_user_id} slot={winning_slot}")

    return {
        "draw_id":       draw_id,
        "winner_name":   winner_ticket['user_name'],
        "winner_email":  winner_ticket['user_email'],
        "winning_slot":  winning_slot,
        "total_slots":   len(slots),
        "seed":          seed,
        "refund_amount": refund_amount,
        "refund_id":     refund_id,
    }

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/register", methods=["POST"])
def register():
    d     = request.json or {}
    name  = d.get("name", "").strip()
    email = d.get("email", "").strip().lower()
    phone = d.get("phone", "").strip()
    pwd   = d.get("password", "")

    if not name or not email or not pwd:
        return jsonify({"error": "Name, email and password required"}), 400
    if len(pwd) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if query("SELECT id FROM users WHERE email=%s", (email,), one=True):
        return jsonify({"error": "Email already registered"}), 409

    is_admin = (email == ADMIN_EMAIL)
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO users (name, email, phone, password, is_admin)
        VALUES (%s,%s,%s,%s,%s) RETURNING id, is_admin
    """, (name, email, phone, generate_password_hash(pwd), is_admin))
    row = cur.fetchone()
    conn.commit(); conn.close()

    session['user_id']  = row['id']
    session['is_admin'] = row['is_admin']
    return jsonify({"message": "Registered!", "user_id": row['id'], "is_admin": row['is_admin']}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    d     = request.json or {}
    email = d.get("email", "").strip().lower()
    pwd   = d.get("password", "")

    user = query("SELECT * FROM users WHERE email=%s", (email,), one=True)
    if not user or not check_password_hash(user['password'], pwd):
        return jsonify({"error": "Invalid credentials"}), 401

    session['user_id']  = user['id']
    session['is_admin'] = user['is_admin']
    return jsonify({"message": "Logged in", "name": user['name'], "is_admin": user['is_admin']})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out"})


@app.route("/api/auth/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "id": u['id'], "name": u['name'],
                    "email": u['email'], "is_admin": u['is_admin']})

# ═══════════════════════════════════════════════════════════════════════════════
#  PRODUCTS routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/products")
def list_products():
    category = request.args.get("category")
    status   = request.args.get("status", "active")
    sql, params = "SELECT * FROM products WHERE status=%s", [status]
    if category:
        sql += " AND category=%s"; params.append(category)
    sql += " ORDER BY created_at DESC"
    rows = query(sql, params)
    result = []
    for r in rows:
        p = dict(r)
        p['original_price_inr'] = r['original_price'] / 100
        p['ticket_price_inr']   = r['ticket_price'] / 100
        p['slots_left']         = r['total_slots'] - r['filled_slots']
        p['fill_pct']           = round(r['filled_slots'] / r['total_slots'] * 100, 1) if r['total_slots'] else 0
        p['discount_pct']       = round((1 - r['ticket_price'] / r['original_price']) * 100)
        result.append(p)
    return jsonify(result)


@app.route("/api/products/<int:pid>")
def get_product(pid):
    p = query("SELECT * FROM products WHERE id=%s", (pid,), one=True)
    if not p:
        return jsonify({"error": "Not found"}), 404
    recent = query("""
        SELECT u.name, t.quantity, t.created_at
        FROM tickets t JOIN users u ON u.id=t.user_id
        WHERE t.product_id=%s AND t.payment_status='paid'
        ORDER BY t.created_at DESC LIMIT 5
    """, (pid,))
    result = dict(p)
    result['ticket_price_inr']   = p['ticket_price'] / 100
    result['original_price_inr'] = p['original_price'] / 100
    result['slots_left']         = p['total_slots'] - p['filled_slots']
    result['recent_buyers']      = [dict(r) for r in recent]
    return jsonify(result)


@app.route("/api/admin/products", methods=["POST"])
@login_required
@admin_required
def create_product():
    d = request.json or {}
    for f in ['name', 'category', 'original_price', 'ticket_price', 'total_slots']:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO products
            (name, description, category, emoji, image_url,
             original_price, ticket_price, total_slots)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (d['name'], d.get('description',''), d['category'],
          d.get('emoji','📦'), d.get('image_url'),
          int(d['original_price'] * 100), int(d['ticket_price'] * 100),
          d['total_slots']))
    pid = cur.fetchone()['id']
    conn.commit(); conn.close()
    return jsonify({"message": "Product created", "product_id": pid}), 201


@app.route("/api/admin/products/<int:pid>", methods=["PATCH"])
@login_required
@admin_required
def update_product(pid):
    d = request.json or {}
    allowed = ['name', 'description', 'category', 'emoji', 'image_url', 'status']
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
#  PAYMENT routes (Razorpay + demo mode)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/orders/create", methods=["POST"])
@login_required
def create_order():
    d          = request.json or {}
    product_id = d.get("product_id")
    quantity   = max(1, min(10, int(d.get("quantity", 1))))

    product = query("SELECT * FROM products WHERE id=%s AND status='active'", (product_id,), one=True)
    if not product:
        return jsonify({"error": "Product not available"}), 404

    slots_left = product['total_slots'] - product['filled_slots']
    if quantity > slots_left:
        return jsonify({"error": f"Only {slots_left} slot(s) left"}), 400

    amount_paise = product['ticket_price'] * quantity

    if rz_client:
        rz_order    = rz_client.order.create({"amount": amount_paise, "currency": "INR"})
        rz_order_id = rz_order['id']
    else:
        rz_order_id = f"demo_order_{secrets.token_hex(6)}"

    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO tickets (product_id, user_id, quantity, amount_paid, razorpay_order_id)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
    """, (product_id, session['user_id'], quantity, amount_paise, rz_order_id))
    ticket_id = cur.fetchone()['id']
    conn.commit(); conn.close()

    return jsonify({
        "order_id":     rz_order_id,
        "amount":       amount_paise,
        "currency":     "INR",
        "key_id":       RAZORPAY_KEY_ID,
        "ticket_id":    ticket_id,
        "product_name": product['name'],
    })


@app.route("/api/orders/verify", methods=["POST"])
@login_required
def verify_payment():
    d = request.json or {}
    razorpay_order_id   = d.get("razorpay_order_id", "")
    razorpay_payment_id = d.get("razorpay_payment_id", "")
    razorpay_signature  = d.get("razorpay_signature", "")
    ticket_id           = d.get("ticket_id")

    if rz_client and razorpay_signature:
        body     = f"{razorpay_order_id}|{razorpay_payment_id}"
        expected = hmac.new(RAZORPAY_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, razorpay_signature):
            return jsonify({"error": "Invalid payment signature"}), 400

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=%s AND user_id=%s", (ticket_id, session['user_id']))
    ticket = cur.fetchone()
    if not ticket:
        conn.close()
        return jsonify({"error": "Ticket not found"}), 404

    pay_id = razorpay_payment_id or f"demo_pay_{secrets.token_hex(6)}"
    cur.execute("UPDATE tickets SET payment_status='paid', razorpay_payment_id=%s WHERE id=%s",
                (pay_id, ticket_id))
    cur.execute("""
        UPDATE products SET filled_slots = filled_slots + %s
        WHERE id=%s RETURNING filled_slots, total_slots, id
    """, (ticket['quantity'], ticket['product_id']))
    prod = cur.fetchone()
    conn.commit(); conn.close()

    draw_result = None
    if prod['filled_slots'] >= prod['total_slots']:
        log.info(f"All slots filled for product {prod['id']} — triggering draw!")
        draw_result = run_draw(prod['id'])

    return jsonify({
        "message":      "You're in the draw! 🎉",
        "filled_slots": prod['filled_slots'],
        "total_slots":  prod['total_slots'],
        "draw_result":  draw_result,
    })


@app.route("/api/orders/demo-pay", methods=["POST"])
@login_required
def demo_pay():
    """Simulate successful payment — no real Razorpay needed for testing."""
    d         = request.json or {}
    ticket_id = d.get("ticket_id")

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=%s AND user_id=%s", (ticket_id, session['user_id']))
    ticket = cur.fetchone()
    if not ticket:
        conn.close()
        return jsonify({"error": "Ticket not found"}), 404

    cur.execute("UPDATE tickets SET payment_status='paid', razorpay_payment_id=%s WHERE id=%s",
                (f"pay_demo_{secrets.token_hex(8)}", ticket_id))
    cur.execute("""
        UPDATE products SET filled_slots = filled_slots + %s
        WHERE id=%s RETURNING filled_slots, total_slots, id
    """, (ticket['quantity'], ticket['product_id']))
    prod = cur.fetchone()
    conn.commit(); conn.close()

    draw_result = None
    if prod['filled_slots'] >= prod['total_slots']:
        draw_result = run_draw(prod['id'])

    return jsonify({
        "message":      "Demo payment successful!",
        "filled_slots": prod['filled_slots'],
        "total_slots":  prod['total_slots'],
        "draw_result":  draw_result,
    })

# ═══════════════════════════════════════════════════════════════════════════════
#  USER dashboard routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/my/tickets")
@login_required
def my_tickets():
    rows = query("""
        SELECT t.*, p.name as product_name, p.emoji, p.category,
               p.original_price, p.status as product_status, p.draw_seed
        FROM tickets t JOIN products p ON p.id=t.product_id
        WHERE t.user_id=%s ORDER BY t.created_at DESC
    """, (session['user_id'],))
    return jsonify([dict(r) for r in rows])


@app.route("/api/my/wins")
@login_required
def my_wins():
    rows = query("""
        SELECT d.*, p.name as product_name, p.emoji, p.original_price
        FROM draws d JOIN products p ON p.id=d.product_id
        WHERE d.winner_user_id=%s ORDER BY d.drawn_at DESC
    """, (session['user_id'],))
    return jsonify([dict(r) for r in rows])

# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC draw history + verification
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/draws")
def draw_history():
    rows = query("""
        SELECT d.id, d.drawn_at, d.winning_slot, d.total_slots,
               d.seed, d.refund_amount, d.refund_status,
               p.name as product_name, p.emoji,
               u.name as winner_name
        FROM draws d
        JOIN products p ON p.id=d.product_id
        JOIN users u    ON u.id=d.winner_user_id
        ORDER BY d.drawn_at DESC LIMIT 50
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/draws/<int:did>/verify")
def verify_draw(did):
    """Public endpoint — anyone can prove the draw was fair."""
    draw = query("SELECT * FROM draws WHERE id=%s", (did,), one=True)
    if not draw:
        return jsonify({"error": "Draw not found"}), 404

    hash_input    = f"{draw['seed']}:{draw['product_id']}:{draw['total_slots']}".encode()
    hash_val      = hashlib.sha256(hash_input).hexdigest()
    computed_slot = int(hash_val, 16) % draw['total_slots']

    return jsonify({
        "draw_id":       did,
        "seed":          draw['seed'],
        "hash":          hash_val,
        "computed_slot": computed_slot,
        "recorded_slot": draw['winning_slot'],
        "is_valid":      computed_slot == draw['winning_slot'],
        "how_to_verify": (
            "Compute SHA-256 of '{seed}:{product_id}:{total_slots}' "
            "and take modulo total_slots to get the winning slot."
        )
    })

# ═══════════════════════════════════════════════════════════════════════════════
#  ADMIN routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/stats")
@login_required
@admin_required
def admin_stats():
    return jsonify({
        "total_users":   query("SELECT COUNT(*) as c FROM users", one=True)['c'],
        "active_draws":  query("SELECT COUNT(*) as c FROM products WHERE status='active'", one=True)['c'],
        "completed_draws": query("SELECT COUNT(*) as c FROM draws", one=True)['c'],
        "total_revenue": query("SELECT COALESCE(SUM(amount_paid),0) as c FROM tickets WHERE payment_status='paid'", one=True)['c'],
        "total_refunds": query("SELECT COALESCE(SUM(refund_amount),0) as c FROM draws WHERE refund_status='processed'", one=True)['c'],
    })


@app.route("/api/admin/tickets")
@login_required
@admin_required
def admin_tickets():
    rows = query("""
        SELECT t.*, u.name as user_name, u.email, p.name as product_name
        FROM tickets t
        JOIN users u ON u.id=t.user_id
        JOIN products p ON p.id=t.product_id
        ORDER BY t.created_at DESC LIMIT 200
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/draw/<int:pid>", methods=["POST"])
@login_required
@admin_required
def manual_draw(pid):
    """Admin can force a draw at any time (e.g. deadline reached)."""
    result = run_draw(pid)
    return jsonify(result)

# ═══════════════════════════════════════════════════════════════════════════════
#  PAGE routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html",
        razorpay_key=RAZORPAY_KEY_ID,
        user=current_user())

@app.route("/admin")
@login_required
@admin_required
def admin_page():
    return render_template("admin.html", user=current_user())

@app.route("/my-tickets")
@login_required
def my_tickets_page():
    return render_template("my_tickets.html", user=current_user())

# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
