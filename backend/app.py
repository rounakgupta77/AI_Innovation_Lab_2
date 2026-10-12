"""Common backend for the Employee and HR reimbursement portals (JSON API on its own port)."""
import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from functools import wraps

from flask import Flask, g, jsonify, request, send_file
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "reimbursement.db")


def _load_env_file():
    """Minimal .env support so the API key can live in backend/.env."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value and not os.environ.get(key.strip()):
                os.environ[key.strip()] = value


_load_env_file()
import anomaly  # noqa: E402
import extractor  # noqa: E402  (reads env vars loaded above)
import sample_history  # noqa: E402

log = logging.getLogger("api")

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".pdf"}
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_FILES_PER_UPLOAD = 25
PORTAL_PORTS = {"5001", "5002"}
BILL_TYPES = extractor.BILL_TYPES

DEMO_USERS = [
    # username, password, name, role, employee code, department
    ("amit", "emp123", "Amit Sharma", "employee", "E1001", "Sales"),
    ("priya", "emp123", "Priya Nair", "employee", "E1002", "Operations"),
    ("rahul", "emp123", "Rahul Verma", "employee", "E1003", "Field Service"),
    ("hr", "hr123", "Neha Kapoor", "hr", "H2001", "Human Resources"),
]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILES_PER_UPLOAD * MAX_FILE_BYTES
app.json.sort_keys = False

executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="extract")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def wipe_all_bills(conn):
    """Deletes every bill (real and sample) and every uploaded bill file. User accounts are kept."""
    count = conn.execute("DELETE FROM bills").rowcount
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'bills'")
    for name in os.listdir(UPLOAD_DIR):
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
        except OSError:
            log.warning("Couldn't delete uploaded file %s", name)
    return count


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('employee', 'hr')),
                employee_code TEXT,
                department TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES users(id),
                file_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                bill_number TEXT NOT NULL DEFAULT '',
                bill_date TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL DEFAULT 0,
                bill_type TEXT NOT NULL DEFAULT '',
                extraction_source TEXT NOT NULL DEFAULT '',
                extraction_note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'processing'
                    CHECK (status IN ('processing', 'draft', 'pending', 'approved', 'rejected')),
                hr_comment TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL,
                submitted_at TEXT,
                decided_at TEXT,
                decided_by INTEGER REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_bills_employee ON bills(employee_id);
            CREATE INDEX IF NOT EXISTS idx_bills_status ON bills(status);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(bills)")}
        for name, ddl in (
            ("anomaly", "INTEGER NOT NULL DEFAULT 0"),
            ("anomaly_reasons", "TEXT NOT NULL DEFAULT '[]'"),
            ("anomaly_checked_at", "TEXT"),
            ("is_sample", "INTEGER NOT NULL DEFAULT 0"),
            ("decision_mode", "TEXT NOT NULL DEFAULT ''"),  # 'manual' (HR clicked) or 'auto' (auto-approved)
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE bills ADD COLUMN {name} {ddl}")
        # Bills decided before auto-approve existed were all decided by HR.
        conn.execute("UPDATE bills SET decision_mode = 'manual' WHERE status IN ('approved', 'rejected')"
                     " AND decision_mode = ''")

        # One-time clean-up requested for this release: start from an empty system.
        if not get_setting(conn, "cleanup_2026_10_12"):
            log.info("One-time clean-up: deleted %d bills and their files", wipe_all_bills(conn))
            set_setting(conn, "cleanup_2026_10_12", now())
        for username, password, name, role, code, dept in DEMO_USERS:
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password_hash, name, role, employee_code, department)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (username, generate_password_hash(password), name, role, code, dept))

        has_samples = conn.execute("SELECT 1 FROM bills WHERE is_sample = 1 LIMIT 1").fetchone()
        if os.environ.get("SAMPLE_HISTORY", "1") == "0":
            if has_samples:
                log.info("Removed %d sample history bills", sample_history.remove(conn))
        elif not has_samples:
            log.info("Added %d sample history bills for anomaly detection", sample_history.seed(conn))

        # Bills sent to HR before anomaly detection existed.
        unchecked = conn.execute(
            "SELECT * FROM bills WHERE status = 'pending' AND anomaly_checked_at IS NULL").fetchall()
        for row in unchecked:
            check_anomaly(conn, row)
        stuck = [r["id"] for r in conn.execute("SELECT id FROM bills WHERE status = 'processing'")]
    for bill_id in stuck:  # server was stopped mid-extraction
        executor.submit(process_bill, bill_id)


# --------------------------------------------------------------------------- #
# Plumbing: CORS, auth, errors
# --------------------------------------------------------------------------- #

@app.after_request
def add_cors(resp):
    origin = request.headers.get("Origin", "")
    m = re.fullmatch(r"https?://[^/:]+:(\d+)", origin)
    if m and m.group(1) in PORTAL_PORTS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    return resp


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify(error=err.message), err.status


@app.errorhandler(413)
def handle_too_large(_):
    return jsonify(error="Upload is too large. Send fewer files at a time."), 413


@app.errorhandler(404)
def handle_404(_):
    return jsonify(error="Not found"), 404


@app.errorhandler(Exception)
def handle_unexpected(err):
    if hasattr(err, "code") and isinstance(getattr(err, "code"), int):
        return jsonify(error=str(err)), err.code
    log.exception("Unhandled error")
    return jsonify(error="Something went wrong on the server."), 500


def require(role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else request.args.get("token", "")
            if not token:
                raise ApiError("Please log in.", 401)
            with db() as conn:
                user = conn.execute(
                    "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
                    (token,)).fetchone()
            if user is None:
                raise ApiError("Your session has expired. Please log in again.", 401)
            if role and user["role"] != role:
                raise ApiError("You don't have access to this.", 403)
            g.user, g.token = user, token
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def public_user(u):
    return {"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role"],
            "employee_code": u["employee_code"], "department": u["department"]}


def bill_json(row, hr=False):
    keys = row.keys()
    out = {
        "id": row["id"],
        "original_name": row["original_name"],
        "file_type": "pdf" if row["file_name"].lower().endswith(".pdf") else "image",
        "bill_number": row["bill_number"],
        "bill_date": row["bill_date"],
        "amount": row["amount"],
        "bill_type": row["bill_type"],
        "extraction_source": row["extraction_source"],
        "extraction_note": row["extraction_note"],
        "status": row["status"],
        "hr_comment": row["hr_comment"],
        "uploaded_at": row["uploaded_at"],
        "submitted_at": row["submitted_at"],
        "decided_at": row["decided_at"],
    }
    if not hr and row["decision_mode"] == "auto":
        out["hr_comment"] = ""  # employees just see "Approved", never how it was decided
    for extra in ("employee_name", "employee_code", "department", "decided_by_name", "duplicate"):
        if extra in keys:
            out[extra] = bool(row[extra]) if extra == "duplicate" else row[extra]
    if hr:
        out["decision_mode"] = row["decision_mode"]
        out["anomaly"] = bool(row["anomaly"])
        try:
            out["anomaly_reasons"] = json.loads(row["anomaly_reasons"] or "[]")
        except ValueError:
            out["anomaly_reasons"] = []
    return out


def check_anomaly(conn, row):
    """Runs anomaly detection on a bill that was just sent to HR and stores the result."""
    try:
        result = anomaly.assess(conn, row)
    except Exception:
        log.exception("Anomaly check failed for bill %s", row["id"])
        result = {"anomaly": False, "reasons": []}
    conn.execute("UPDATE bills SET anomaly = ?, anomaly_reasons = ?, anomaly_checked_at = ? WHERE id = ?",
                 (int(result["anomaly"]), json.dumps(result["reasons"], ensure_ascii=False), now(), row["id"]))
    return result


# --------------------------------------------------------------------------- #
# Field validation
# --------------------------------------------------------------------------- #

def clean_fields(data, strict):
    """Validates editable bill fields. strict=True when sending to HR (everything required)."""
    out = {}
    if "bill_number" in data:
        value = str(data["bill_number"] or "").strip()
        if len(value) > 64:
            raise ApiError("Bill number is too long.")
        out["bill_number"] = value
    if "bill_date" in data:
        value = str(data["bill_date"] or "").strip()
        if value:
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                raise ApiError("Bill date must be a valid date.")
            if parsed > date.today():
                raise ApiError("Bill date can't be in the future.")
            value = parsed.isoformat()
        out["bill_date"] = value
    if "amount" in data:
        raw = data["amount"]
        try:
            value = round(float(str(raw).replace(",", "")) if raw not in (None, "") else 0.0, 2)
        except ValueError:
            raise ApiError("Amount must be a number.")
        if value < 0 or value > 10_000_000 or value != value:
            raise ApiError("Amount looks invalid.")
        out["amount"] = value
    if "bill_type" in data:
        value = str(data["bill_type"] or "").strip()
        if value and value not in BILL_TYPES:
            raise ApiError("Unknown bill type.")
        out["bill_type"] = value

    if strict:
        if not out.get("bill_type"):
            raise ApiError("Choose the bill type before sending.")
        if not out.get("bill_number"):
            raise ApiError("Bill number is required before sending.")
        if not out.get("bill_date"):
            raise ApiError("Bill date is required before sending.")
        if not out.get("amount"):
            raise ApiError("Amount must be greater than zero before sending.")
    return out


# --------------------------------------------------------------------------- #
# AI extraction worker
# --------------------------------------------------------------------------- #

def process_bill(bill_id):
    with db() as conn:
        row = conn.execute("SELECT file_name FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if row is None:
        return
    try:
        data = extractor.extract(os.path.join(UPLOAD_DIR, row["file_name"]))
    except Exception as exc:
        log.exception("Extraction crashed for bill %s", bill_id)
        data = {"bill_number": "", "bill_date": "", "amount": 0.0, "bill_type": "",
                "source": "manual", "note": f"Couldn't read this bill ({type(exc).__name__}), please enter the details."}

    bill_date = data.get("bill_date") or ""
    try:
        if bill_date and date.fromisoformat(bill_date) > date.today():
            bill_date = ""
    except ValueError:
        bill_date = ""
    bill_type = data.get("bill_type") if data.get("bill_type") in BILL_TYPES else ""

    with db() as conn:
        conn.execute(
            "UPDATE bills SET bill_number = ?, bill_date = ?, amount = ?, bill_type = ?,"
            " extraction_source = ?, extraction_note = ?, status = 'draft'"
            " WHERE id = ? AND status = 'processing'",
            (str(data.get("bill_number") or "")[:64], bill_date, float(data.get("amount") or 0),
             bill_type, data.get("source", "manual"), data.get("note", ""), bill_id))


# --------------------------------------------------------------------------- #
# Routes: auth + config
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health():
    return jsonify(ok=True, extraction=extractor.capabilities())


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    portal = data.get("portal")
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            raise ApiError("Wrong username or password.", 401)
        if portal and user["role"] != portal:
            where = "HR portal" if user["role"] == "hr" else "Employee portal"
            raise ApiError(f"This account belongs to the {where}.", 403)
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                     (token, user["id"], now()))
    return jsonify(token=token, user=public_user(user))


@app.post("/api/logout")
@require()
def logout():
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (g.token,))
    return jsonify(ok=True)


@app.get("/api/me")
@require()
def me():
    return jsonify(user=public_user(g.user), extraction=extractor.capabilities())


# --------------------------------------------------------------------------- #
# Routes: employee
# --------------------------------------------------------------------------- #

def own_bill(conn, bill_id):
    row = conn.execute("SELECT * FROM bills WHERE id = ? AND employee_id = ? AND is_sample = 0",
                       (bill_id, g.user["id"])).fetchone()
    if row is None:
        raise ApiError("Bill not found.", 404)
    return row


@app.get("/api/my/bills")
@require("employee")
def my_bills():
    with db() as conn:
        rows = conn.execute("SELECT * FROM bills WHERE employee_id = ? AND is_sample = 0 ORDER BY id DESC",
                            (g.user["id"],)).fetchall()
    return jsonify(bills=[bill_json(r) for r in rows])


@app.post("/api/my/bills")
@require("employee")
def upload_bills():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        raise ApiError("Choose at least one bill to upload.")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise ApiError(f"You can upload up to {MAX_FILES_PER_UPLOAD} bills at a time.")

    accepted, rejected = [], []
    for f in files:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            rejected.append(f"{f.filename}: only images (JPG, PNG, WEBP...) and PDFs are allowed")
            continue
        content = f.read()
        if not content:
            rejected.append(f"{f.filename}: file is empty")
            continue
        if len(content) > MAX_FILE_BYTES:
            rejected.append(f"{f.filename}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
            continue
        if ext == ".pdf" and not content.startswith(b"%PDF"):
            rejected.append(f"{f.filename}: not a valid PDF")
            continue
        stored = f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_DIR, stored), "wb") as out:
            out.write(content)
        accepted.append((stored, os.path.basename(f.filename)[:200]))

    created = []
    with db() as conn:
        for stored, original in accepted:
            cur = conn.execute(
                "INSERT INTO bills (employee_id, file_name, original_name, uploaded_at) VALUES (?, ?, ?, ?)",
                (g.user["id"], stored, original, now()))
            created.append(cur.lastrowid)
    for bill_id in created:
        executor.submit(process_bill, bill_id)

    if not created:
        raise ApiError("None of the files could be uploaded. " + "; ".join(rejected))
    return jsonify(created=created, rejected=rejected), 201


@app.patch("/api/my/bills/<int:bill_id>")
@require("employee")
def edit_bill(bill_id):
    fields = clean_fields(request.get_json(silent=True) or {}, strict=False)
    with db() as conn:
        row = own_bill(conn, bill_id)
        if row["status"] != "draft":
            raise ApiError("Only bills that haven't been sent can be edited.", 409)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE bills SET {sets} WHERE id = ?", (*fields.values(), bill_id))
        row = own_bill(conn, bill_id)
    return jsonify(bill=bill_json(row))


@app.delete("/api/my/bills/<int:bill_id>")
@require("employee")
def delete_bill(bill_id):
    with db() as conn:
        row = own_bill(conn, bill_id)
        if row["status"] not in ("draft", "processing"):
            raise ApiError("Bills already sent to HR can't be deleted.", 409)
        conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
    try:
        os.remove(os.path.join(UPLOAD_DIR, row["file_name"]))
    except OSError:
        pass
    return jsonify(ok=True)


@app.post("/api/my/bills/<int:bill_id>/send")
@require("employee")
def send_bill(bill_id):
    body = request.get_json(silent=True) or {}
    with db() as conn:
        row = own_bill(conn, bill_id)
        if row["status"] == "processing":
            raise ApiError("This bill is still being read. Try again in a moment.", 409)
        if row["status"] != "draft":
            raise ApiError("This bill has already been sent to HR.", 409)
        merged = {k: body.get(k, row[k]) for k in ("bill_number", "bill_date", "amount", "bill_type")}
        fields = clean_fields(merged, strict=True)

        dup = conn.execute(
            "SELECT id, status FROM bills WHERE employee_id = ? AND id != ? AND status IN ('pending', 'approved')"
            " AND lower(bill_number) = lower(?) AND bill_date = ? AND abs(amount - ?) < 0.01",
            (g.user["id"], bill_id, fields["bill_number"], fields["bill_date"], fields["amount"])).fetchone()
        if dup:
            raise ApiError(f"You already sent this bill (same number, date and amount), "
                           f"it is {'approved' if dup['status'] == 'approved' else 'pending with HR'}.", 409)

        conn.execute(
            "UPDATE bills SET bill_number = ?, bill_date = ?, amount = ?, bill_type = ?,"
            " status = 'pending', submitted_at = ? WHERE id = ?",
            (fields["bill_number"], fields["bill_date"], fields["amount"], fields["bill_type"], now(), bill_id))
        result = check_anomaly(conn, own_bill(conn, bill_id))
        if get_setting(conn, "auto_approve") == "1" and not result["anomaly"]:
            # Possible duplicates (same number + amount as another employee's bill) still go to a person.
            dup_elsewhere = conn.execute(
                "SELECT 1 FROM bills WHERE id != ? AND is_sample = 0 AND status IN ('pending', 'approved', 'rejected')"
                " AND lower(bill_number) = lower(?) AND abs(amount - ?) < 0.01",
                (bill_id, fields["bill_number"], fields["amount"])).fetchone()
            if not dup_elsewhere:
                conn.execute(
                    "UPDATE bills SET status = 'approved', decision_mode = 'auto', decided_at = ?,"
                    " hr_comment = 'Auto-approved: no anomaly detected' WHERE id = ? AND status = 'pending'",
                    (now(), bill_id))
        row = own_bill(conn, bill_id)
    return jsonify(bill=bill_json(row))


# --------------------------------------------------------------------------- #
# Routes: HR
# --------------------------------------------------------------------------- #

HR_SELECT = """
    SELECT b.*, u.name AS employee_name, u.employee_code, u.department, d.name AS decided_by_name,
        EXISTS (
            SELECT 1 FROM bills o
            WHERE o.id != b.id AND o.is_sample = 0 AND o.status IN ('pending', 'approved', 'rejected')
              AND o.bill_number != '' AND lower(o.bill_number) = lower(b.bill_number)
              AND abs(o.amount - b.amount) < 0.01
        ) AS duplicate
    FROM bills b
    JOIN users u ON u.id = b.employee_id
    LEFT JOIN users d ON d.id = b.decided_by
"""


HISTORY_LIMIT = 1000


def auto_approve_state(conn):
    return {"enabled": get_setting(conn, "auto_approve") == "1",
            "since": get_setting(conn, "auto_approve_since") or None}


@app.get("/api/hr/bills")
@require("hr")
def hr_bills():
    """Pending bills (oldest first) and the decision history (newest first, never removed)."""
    only_anomalies = request.args.get("anomalies") == "1"
    extra = " AND b.anomaly = 1" if only_anomalies else ""
    with db() as conn:
        pending = conn.execute(
            f"{HR_SELECT} WHERE b.is_sample = 0 AND b.status = 'pending'{extra} ORDER BY b.submitted_at ASC"
        ).fetchall()
        history = conn.execute(
            f"{HR_SELECT} WHERE b.is_sample = 0 AND b.status IN ('approved', 'rejected'){extra}"
            f" ORDER BY b.decided_at DESC, b.id DESC LIMIT {HISTORY_LIMIT}").fetchall()
        stats = conn.execute("""
            SELECT
                SUM(status = 'pending') AS pending_count,
                COALESCE(SUM(CASE WHEN status = 'pending' THEN amount END), 0) AS pending_amount,
                SUM(status = 'approved') AS approved_count,
                COALESCE(SUM(CASE WHEN status = 'approved' THEN amount END), 0) AS approved_amount,
                SUM(status = 'rejected') AS rejected_count,
                SUM(status = 'approved' AND decision_mode = 'auto') AS auto_count,
                SUM(status = 'pending' AND anomaly = 1) AS anomaly_pending,
                SUM(anomaly = 1) AS anomaly_total
            FROM bills WHERE is_sample = 0 AND status IN ('pending', 'approved', 'rejected')
        """).fetchone()
        detection = anomaly.info(conn)
        auto = auto_approve_state(conn)
    return jsonify(pending=[bill_json(r, hr=True) for r in pending],
                   history=[bill_json(r, hr=True) for r in history],
                   stats={k: (stats[k] or 0) for k in stats.keys()},
                   detection=detection, auto_approve=auto)


@app.post("/api/hr/auto-approve")
@require("hr")
def hr_set_auto_approve():
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError("enabled must be true or false.")
    with db() as conn:
        was = get_setting(conn, "auto_approve") == "1"
        set_setting(conn, "auto_approve", "1" if enabled else "0")
        if enabled and not was:
            set_setting(conn, "auto_approve_since", now())  # only bills sent from now on are auto-approved
        state = auto_approve_state(conn)
    return jsonify(auto_approve=state)


@app.post("/api/hr/bills/<int:bill_id>/decision")
@require("hr")
def hr_decide(bill_id):
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    comment = str(data.get("comment") or "").strip()[:500]
    if action not in ("approve", "reject"):
        raise ApiError("Action must be approve or reject.")
    if action == "reject" and not comment:
        raise ApiError("Please give a reason for rejecting the bill.")
    new_status = "approved" if action == "approve" else "rejected"
    with db() as conn:
        cur = conn.execute(
            "UPDATE bills SET status = ?, hr_comment = ?, decided_at = ?, decided_by = ?, decision_mode = 'manual'"
            " WHERE id = ? AND status = 'pending' AND is_sample = 0",
            (new_status, comment, now(), g.user["id"], bill_id))
        if cur.rowcount == 0:
            exists = conn.execute("SELECT status FROM bills WHERE id = ?", (bill_id,)).fetchone()
            if exists is None or exists["status"] in ("draft", "processing"):
                raise ApiError("Bill not found.", 404)
            raise ApiError(f"This bill was already {exists['status']}.", 409)
        row = conn.execute(f"{HR_SELECT} WHERE b.id = ?", (bill_id,)).fetchone()
    return jsonify(bill=bill_json(row, hr=True))


# --------------------------------------------------------------------------- #
# Routes: bill file (owner or HR)
# --------------------------------------------------------------------------- #

@app.get("/api/bills/<int:bill_id>/file")
@require()
def bill_file(bill_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM bills WHERE id = ? AND is_sample = 0", (bill_id,)).fetchone()
    allowed = row is not None and (
        (g.user["role"] == "employee" and row["employee_id"] == g.user["id"])
        or (g.user["role"] == "hr" and row["status"] in ("pending", "approved", "rejected")))
    if not allowed:
        raise ApiError("Bill not found.", 404)
    path = os.path.join(UPLOAD_DIR, row["file_name"])
    if not os.path.exists(path):
        raise ApiError("The bill file is missing on the server.", 404)
    return send_file(path, download_name=row["original_name"], max_age=0)


init_db()
