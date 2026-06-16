import os
import json
import pyodbc
import logging
from datetime import datetime
from functools import wraps

import bcrypt
from flask import Blueprint, request, jsonify
from flask_login import (
    LoginManager, UserMixin,
    login_user, logout_user, login_required, current_user,
)

logger = logging.getLogger(__name__)
bp = Blueprint("auth", __name__)
login_manager = LoginManager()

KNOWN_AGENT_PERMS = ["device_reset", "unpick"]
VALID_ROLES = ("superadmin", "admin", "user")


# ── MSSQL helper classes and connection provider ──────────────────────────────

class MSSQLConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        cursor = self.conn.cursor()
        if params is not None:
            # Standardize parameters as sequence to support pyodbc execution
            if not isinstance(params, (list, tuple)):
                params = (params,)
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        return cursor

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def _get_conn():
    conn_str = os.getenv("METADATA_DB_CONN_STR")
    if not conn_str:
        server = os.getenv("METADATA_DB_SERVER", "localhost")
        database = os.getenv("METADATA_DB_DATABASE", "tychons_forge")
        user = os.getenv("METADATA_DB_USER")
        password = os.getenv("METADATA_DB_PASSWORD")
        driver = os.getenv("METADATA_DB_DRIVER", "SQL Server")
        
        if user and password:
            conn_str = f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={user};PWD={password}"
        else:
            conn_str = f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"
            
        if "ODBC Driver 18" in driver:
            conn_str += ";TrustServerCertificate=yes"
            
    conn = pyodbc.connect(conn_str, timeout=10)
    return MSSQLConnectionWrapper(conn)



def init_db():
    conn = _get_conn()
    
    # Check if table exists in SQL Server
    table_exists_query = """
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES 
        WHERE TABLE_NAME = 'users'
    """
    cursor = conn.execute(table_exists_query)
    table_exists = cursor.fetchone()[0] > 0
    
    if not table_exists:
        # Create users table in SQL Server
        create_table_query = """
            CREATE TABLE users (
                id                    INT IDENTITY(1,1) PRIMARY KEY,
                username              NVARCHAR(150) UNIQUE NOT NULL,
                password_hash         NVARCHAR(255) NOT NULL,
                role                  NVARCHAR(50) NOT NULL DEFAULT 'user',
                agent_perms           NVARCHAR(1000) NOT NULL DEFAULT '[]',
                is_active             INT NOT NULL DEFAULT 1,
                created_at            NVARCHAR(100),
                last_login            NVARCHAR(100),
                display_name          NVARCHAR(255),
                force_change_password INT NOT NULL DEFAULT 1
            )
        """
        conn.execute(create_table_query)
        conn.commit()
        logger.info("Created users table in MSSQL database.")
    else:
        # Migration: check if display_name column exists
        col_exists_query = """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'display_name'
        """
        cursor = conn.execute(col_exists_query)
        if cursor.fetchone()[0] == 0:
            conn.execute("ALTER TABLE users ADD display_name NVARCHAR(255)")
            conn.commit()
            logger.info("Added display_name column to users table.")

        # Migration: check if force_change_password column exists
        col_exists_query_2 = """
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_NAME = 'users' AND COLUMN_NAME = 'force_change_password'
        """
        cursor = conn.execute(col_exists_query_2)
        if cursor.fetchone()[0] == 0:
            conn.execute("ALTER TABLE users ADD force_change_password INT NOT NULL DEFAULT 1")
            conn.commit()
            logger.info("Added force_change_password column to users table.")

    # Check user count
    cursor = conn.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        rounds = int(os.getenv("BCRYPT_ROUNDS", "12"))
        pw_hash = bcrypt.hashpw(b"change-me", bcrypt.gensalt(rounds=rounds)).decode()
        conn.execute(
            "INSERT INTO users (username, password_hash, role, agent_perms, is_active, created_at, display_name, force_change_password)"
            " VALUES (?, ?, 'superadmin', '[]', 1, ?, 'Administrator', 1)",
            ("admin", pw_hash, datetime.utcnow().isoformat()),
        )
        conn.commit()
        logger.warning("Default superadmin created — login: admin / change-me.")

    # Ensure job_logs table exists
    job_logs_exists = conn.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'job_logs'"
    ).fetchone()[0] > 0
    if not job_logs_exists:
        conn.execute("""
            CREATE TABLE job_logs (
                id           INT IDENTITY(1,1) PRIMARY KEY,
                log_type     NVARCHAR(50) NOT NULL,
                run_id       NVARCHAR(100) NOT NULL,
                timestamp    NVARCHAR(100) NOT NULL,
                level        NVARCHAR(50) NOT NULL,
                device_id    NVARCHAR(255),
                wh_id        NVARCHAR(50),
                order_number NVARCHAR(100),
                item_number  NVARCHAR(100),
                message      NVARCHAR(MAX) NOT NULL
            )
        """)
        conn.commit()
        logger.info("Created job_logs table.")

    # Ensure audit_logs table exists
    audit_logs_exists = conn.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'audit_logs'"
    ).fetchone()[0] > 0
    if not audit_logs_exists:
        conn.execute("""
            CREATE TABLE audit_logs (
                id        INT IDENTITY(1,1) PRIMARY KEY,
                username  NVARCHAR(150) NOT NULL,
                timestamp NVARCHAR(100) NOT NULL,
                action    NVARCHAR(100) NOT NULL,
                target    NVARCHAR(255) NOT NULL,
                details   NVARCHAR(MAX) NOT NULL
            )
        """)
        conn.commit()
        logger.info("Created audit_logs table.")

    conn.close()


# ── User model ────────────────────────────────────────────────────────────────

class User(UserMixin):
    def __init__(self, row):
        self.id                    = row[0]
        self.username              = row[1]
        self._pw_hash              = row[2]
        self.role                  = row[3]
        self.agent_perms           = json.loads(row[4] or "[]")
        self._is_active            = bool(row[5])
        # row[6]=created_at, row[7]=last_login, row[8]=display_name, row[9]=force_change_password
        self.display_name          = (row[8] if len(row) > 8 and row[8] else None) or row[1]
        self.force_change_password = bool(row[9]) if len(row) > 9 else False

    def get_id(self):
        return str(self.id)

    @property
    def is_active(self):
        return self._is_active

    def is_superadmin(self):
        return self.role == "superadmin"

    def is_admin(self):
        return self.role in ("admin", "superadmin")

    def has_agent_perm(self, perm):
        return self.is_admin() or perm in self.agent_perms


@login_manager.user_loader
def _load_user(user_id):
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT id, username, password_hash, role, agent_perms, is_active, created_at, last_login, display_name, force_change_password"
            " FROM users WHERE id = ? AND is_active = 1",
            (int(user_id),),
        ).fetchone()
        conn.close()
        return User(row) if row else None
    except Exception:
        return None


# ── RBAC decorators ───────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            return jsonify({"type": "error", "error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated


def superadmin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_superadmin():
            return jsonify({"type": "error", "error": "Superadmin access required"}), 403
        return f(*args, **kwargs)
    return decorated


def require_agent(perm):
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated(*args, **kwargs):
            if not current_user.has_agent_perm(perm):
                return jsonify({"type": "error", "error": f"Permission denied: {perm}"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def admin_required_check():
    """Inline auth check (used to avoid circular decorator imports)."""
    if not current_user.is_authenticated:
        return jsonify({"type": "error", "error": "Not authenticated"}), 401
    if not current_user.is_admin():
        return jsonify({"type": "error", "error": "Admin access required"}), 403
    return None


def superadmin_required_check():
    if not current_user.is_authenticated:
        return jsonify({"type": "error", "error": "Not authenticated"}), 401
    if not current_user.is_superadmin():
        return jsonify({"type": "error", "error": "Superadmin access required"}), 403
    return None


@bp.before_app_request
def check_forced_password_change():
    if current_user.is_authenticated:
        if getattr(current_user, "force_change_password", False):
            path = request.path
            if path in (
                "/api/v0/auth/profile",
                "/api/v0/auth/logout",
                "/api/v0/auth/me"
            ):
                return None
            if path.startswith("/api/v0/"):
                return jsonify({
                    "type": "error",
                    "error": "Password change required",
                    "force_password_change": True
                }), 403


# ── Auth routes ───────────────────────────────────────────────────────────────
from extensions import limiter

@bp.route("/api/v0/auth/login", methods=["POST"])
@limiter.limit("5 per minute")
def login():
    data     = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or not password:
        return jsonify({"type": "error", "error": "Username and password required"}), 400

    conn = _get_conn()
    row = conn.execute(
        "SELECT id, username, password_hash, role, agent_perms, is_active, created_at, last_login, display_name, force_change_password"
        " FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if not row or not row[5]:
        conn.close()
        log_audit_action(username or "unknown", "LOGIN_FAILURE", "auth", {"attempted_username": username, "reason": "user_not_found_or_inactive"})
        return jsonify({"type": "error", "error": "Invalid credentials"}), 401
    if not bcrypt.checkpw(password.encode(), row[2].encode()):
        conn.close()
        log_audit_action(username, "LOGIN_FAILURE", "auth", {"attempted_username": username, "reason": "incorrect_password"})
        return jsonify({"type": "error", "error": "Invalid credentials"}), 401

    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().isoformat(), row[0]))
    conn.commit()
    conn.close()

    user = User(row)
    from flask import session
    session.permanent = True
    login_user(user)
    log_audit_action(user.username, "LOGIN_SUCCESS", "auth", {"role": user.role})
    return jsonify({
        "type": "success",
        "user": {
            "id":                    user.id,
            "username":              user.username,
            "display_name":          user.display_name,
            "role":                  user.role,
            "agent_perms":           user.agent_perms,
            "force_change_password": user.force_change_password,
        },
    })


@bp.route("/api/v0/auth/logout", methods=["POST"])
@login_required
def logout():
    log_audit_action(current_user.username, "LOGOUT", "auth", {})
    logout_user()
    return jsonify({"type": "success"})


@bp.route("/api/v0/auth/me", methods=["GET"])
@login_required
def me():
    return jsonify({
        "id":                    current_user.id,
        "username":              current_user.username,
        "display_name":          current_user.display_name,
        "role":                  current_user.role,
        "agent_perms":           current_user.agent_perms,
        "force_change_password": current_user.force_change_password,
    })


@bp.route("/api/v0/auth/profile", methods=["PATCH"])
@login_required
def update_profile():
    data    = request.get_json() or {}
    conn    = _get_conn()
    updates, params = [], []

    if "display_name" in data:
        dn = data["display_name"].strip()
        if dn:
            updates.append("display_name = ?")
            params.append(dn)

    if data.get("new_password"):
        if not data.get("current_password"):
            conn.close()
            return jsonify({"type": "error", "error": "Current password required"}), 400
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (current_user.id,)).fetchone()
        if not bcrypt.checkpw(data["current_password"].encode(), row[0].encode()):
            conn.close()
            return jsonify({"type": "error", "error": "Current password is incorrect"}), 400
        rounds  = int(os.getenv("BCRYPT_ROUNDS", "12"))
        pw_hash = bcrypt.hashpw(data["new_password"].encode(), bcrypt.gensalt(rounds=rounds)).decode()
        updates.append("password_hash = ?")
        params.append(pw_hash)
        updates.append("force_change_password = ?")
        params.append(0)

    if updates:
        params.append(current_user.id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        
        details = {}
        if "display_name" in data:
            details["display_name"] = data["display_name"].strip()
        if data.get("new_password"):
            details["password_changed"] = True
            
        log_audit_action(
            current_user.username,
            "PASSWORD_CHANGE" if data.get("new_password") else "UPDATE_PROFILE",
            "self",
            details
        )
    conn.close()
    return jsonify({"type": "success"})


@bp.route("/api/v0/admin/audit_logs", methods=["GET"])
def get_audit_logs():
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err

    conn = _get_conn()
    cursor = conn.execute(
        "SELECT id, username, timestamp, action, target, details FROM audit_logs ORDER BY id DESC"
    )
    rows = cursor.fetchall()
    conn.close()

    logs = []
    for r in rows:
        try:
            details = json.loads(r[5])
        except Exception:
            details = {"raw": r[5]}
        logs.append({
            "id": r[0],
            "username": r[1],
            "timestamp": r[2],
            "action": r[3],
            "target": r[4],
            "details": details
        })
    return jsonify({"audit_logs": logs})


@bp.route("/api/v0/admin/audit_logs/download", methods=["GET"])
def download_audit_logs():
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err

    fmt = request.args.get("format", "csv").lower()
    conn = _get_conn()
    cursor = conn.execute(
        "SELECT id, username, timestamp, action, target, details FROM audit_logs ORDER BY id DESC"
    )
    rows = cursor.fetchall()
    conn.close()

    from flask import Response
    if fmt == "csv":
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Username", "Timestamp (UTC)", "Action", "Target", "IP Address", "User Agent", "Details"])
        for r in rows:
            try:
                details = json.loads(r[5])
            except Exception:
                details = {"raw": r[5]}
            ip = details.pop("ip_address", "")
            ua = details.pop("user_agent", "")
            writer.writerow([r[0], r[1], r[2], r[3], r[4], ip, ua, json.dumps(details)])
        
        response = Response(output.getvalue(), mimetype="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=audit_logs.csv"
        return response
    else:
        output = []
        for r in rows:
            try:
                details = json.loads(r[5])
            except Exception:
                details = {"raw": r[5]}
            ip = details.pop("ip_address", "—")
            ua = details.pop("user_agent", "—")
            output.append(
                f"[{r[2]}] USER: {r[1]} | ACTION: {r[3]} | TARGET: {r[4]} | IP: {ip}\n"
                f"  UA: {ua}\n"
                f"  DETAILS: {json.dumps(details)}\n"
                f"{'-'*80}"
            )
        response = Response("\n".join(output), mimetype="text/plain")
        response.headers["Content-Disposition"] = "attachment; filename=audit_logs.txt"
        return response


from flask import has_request_context

def log_audit_action(username: str, action: str, target: str, details: dict):
    try:
        enriched_details = dict(details)
        if has_request_context():
            enriched_details["ip_address"] = request.headers.get("X-Forwarded-For", request.remote_addr)
            enriched_details["user_agent"] = request.headers.get("User-Agent", "")
        details_str = json.dumps(enriched_details)
        conn = _get_conn()
        conn.execute(
            "INSERT INTO audit_logs (username, timestamp, action, target, details)"
            " VALUES (?, ?, ?, ?, ?)",
            (username, datetime.utcnow().isoformat(), action, target, details_str)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Failed to log audit action: %s", e)

