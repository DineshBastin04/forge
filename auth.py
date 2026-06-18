import os
import re
import yaml
import json
import pyodbc
import logging
from datetime import datetime
from functools import wraps

import bcrypt
from flask import Blueprint, request, jsonify, has_request_context
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


_SQLSERVER_DRIVER_PREFERENCE = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
]


def _resolve_driver(requested: str) -> str:
    """Return `requested` if installed, otherwise fall back to the best available SQL Server driver."""
    installed = set(pyodbc.drivers())
    if requested in installed:
        return requested
    for candidate in _SQLSERVER_DRIVER_PREFERENCE:
        if candidate in installed:
            logger.warning(
                "ODBC driver '%s' not found — using '%s' instead. "
                "Set METADATA_DB_DRIVER in .env to suppress this warning.",
                requested, candidate,
            )
            return candidate
    raise RuntimeError(
        f"No SQL Server ODBC driver found on this machine. "
        f"Installed drivers: {sorted(installed)}. "
        f"Install 'ODBC Driver 17 for SQL Server' or newer."
    )


def _build_conn_str_meta(server, database, user, password, driver, trusted=False):
    trust = ";TrustServerCertificate=yes" if any(v in driver for v in ("ODBC Driver 17", "ODBC Driver 18")) else ""
    if not trusted and user and password:
        return f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={user};PWD={password}{trust}"
    return f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;{trust}"


def _try_connect(conn_str: str, fallback_conn_str: str | None = None):
    """Attempt pyodbc.connect; if SQL auth fails (28000) and a fallback is given, try that."""
    try:
        return pyodbc.connect(conn_str, timeout=10)
    except pyodbc.Error as e:
        code = e.args[0] if e.args else ""
        if code == "28000" and fallback_conn_str:
            logger.warning(
                "SQL auth failed (%s) — retrying with Windows Authentication.",
                e.args[1] if len(e.args) > 1 else e,
            )
            return pyodbc.connect(fallback_conn_str, timeout=10)
        raise


def _ensure_db(server, database, user, password, driver):
    """Connect to master and CREATE the target database if it does not exist.

    Returns True if created/verified, False if master was inaccessible — caller
    falls back to a direct connection attempt.
    """
    master_str  = _build_conn_str_meta(server, "master", user, password, driver)
    trusted_str = _build_conn_str_meta(server, "master", user, password, driver, trusted=True)
    try:
        conn = _try_connect(master_str, trusted_str)
    except pyodbc.Error as e:
        code = e.args[0] if e.args else ""
        if code in ("28000", "IM002"):
            logger.warning(
                "Cannot connect to 'master' (%s). "
                "Skipping auto-create — the database must already exist.",
                e.args[1] if len(e.args) > 1 else e,
            )
            return False
        raise
    conn.autocommit = True
    safe_db = database.replace("'", "''").replace("]", "]]")
    conn.cursor().execute(
        f"IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = N'{safe_db}')"
        f" CREATE DATABASE [{safe_db}]"
    )
    conn.close()
    logger.info("Database '%s' is ready.", database)
    return True


def _get_conn():
    conn_str = os.getenv("METADATA_DB_CONN_STR")
    if not conn_str:
        server   = os.getenv("METADATA_DB_SERVER",   "localhost")
        database = os.getenv("METADATA_DB_DATABASE", "tychons_wi_agents")
        user     = os.getenv("METADATA_DB_USER")
        password = os.getenv("METADATA_DB_PASSWORD")
        driver   = _resolve_driver(os.getenv("METADATA_DB_DRIVER", "SQL Server"))

        _ensure_db(server, database, user, password, driver)
        conn_str         = _build_conn_str_meta(server, database, user, password, driver)
        trusted_conn_str = _build_conn_str_meta(server, database, user, password, driver, trusted=True)
    else:
        trusted_conn_str = None

    try:
        conn = _try_connect(conn_str, trusted_conn_str)
    except pyodbc.Error as e:
        code = e.args[0] if e.args else ""
        if code == "42000" and "4060" in str(e):
            db = os.getenv("METADATA_DB_DATABASE", "tychons_wi_agents")
            raise RuntimeError(
                f"Database '{db}' does not exist and could not be auto-created "
                f"(the login lacks access to 'master'). "
                f"Create it manually in SQL Server Management Studio, "
                f"or use a login with the 'dbcreator' server role."
            ) from e
        raise
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
            ("superadmin", pw_hash, datetime.utcnow().isoformat()),
        )
        conn.commit()
        logger.warning("Default superadmin created — login: superadmin / change-me.")

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

    # Ensure agents table exists
    agents_exists = conn.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'agents'"
    ).fetchone()[0] > 0
    if not agents_exists:
        conn.execute("""
            CREATE TABLE agents (
                id          NVARCHAR(50) PRIMARY KEY,
                name        NVARCHAR(100) NOT NULL,
                description NVARCHAR(255),
                flow_yaml   NVARCHAR(MAX)
            )
        """)
        conn.commit()
        logger.info("Created agents table.")

    device_reset_yaml = """queries:
  auto_scan: "SELECT device_id FROM t_log_message WHERE details LIKE '%Data Error%' AND device_id IS NOT NULL AND user_id IS NOT NULL AND logged_on_utc >= DATEADD(HOUR, -2, GETUTCDATE()) AND logged_on_utc < GETUTCDATE()"
  find_employee: "SELECT id FROM t_employee WHERE device = :dev"
  find_location: "SELECT wh_id, location_id FROM t_location WHERE c1 = :emp"
  check_stored_item: "SELECT 1 FROM t_stored_item WHERE location_id = :l AND wh_id = :w"
  check_hu_master: "SELECT 1 FROM t_hu_master WHERE location_id = :l AND wh_id = :w"
  find_staging: "SELECT TOP 1 tl.location_id FROM t_location tl (NOLOCK) WHERE tl.wh_id = :wh AND (tl.status = 'E' OR tl.status = 'P') AND tl.type = 'S' AND (tl.description LIKE '%STAGE%' OR tl.description LIKE '%STAGING%') AND NOT EXISTS (SELECT 1 FROM t_stored_item si WHERE si.location_id = tl.location_id AND si.wh_id = tl.wh_id) AND NOT EXISTS (SELECT 1 FROM t_hu_master hm WHERE hm.location_id = tl.location_id AND hm.wh_id = tl.wh_id) ORDER BY tl.status ASC, ISNULL(tl.stored_qty, 0) ASC"
  update_employee: "UPDATE t_employee SET device = NULL WHERE id = :id AND wh_id = :wh AND device = :dev"
  update_location: "UPDATE t_location SET c1 = NULL, status = 'E' WHERE location_id = :loc AND wh_id = :wh"
  find_employee_by_id: "SELECT id FROM t_employee WHERE id = :id"
  find_device_by_employee: "SELECT device FROM t_employee WHERE id = :id"
"""
    unpick_yaml = """queries:
  auto_scan: "SELECT DISTINCT TL.control_number AS order_number, TL.wh_id, TL.item_number FROM t_tran_log TL WITH(NOLOCK) LEFT JOIN t_pick_detail PD ON PD.order_number = TL.control_number AND PD.wh_id = TL.wh_id AND PD.item_number = TL.item_number AND PD.line_number = TL.line_number LEFT JOIN t_stored_item SI ON SI.wh_id = TL.wh_id AND SI.item_number = TL.item_number AND SI.type = TL.control_number LEFT JOIN t_hu_master HM ON HM.wh_id = TL.wh_id AND HM.hu_id = TL.hu_id LEFT JOIN t_hu_detail HD ON HD.wh_id = TL.wh_id AND HD.hu_id = TL.hu_id AND HD.item_number = TL.item_number LEFT JOIN t_work_q WQ ON WQ.wh_id = TL.wh_id AND WQ.pick_ref_number = TL.control_number AND WQ.item_number = TL.item_number WHERE TL.tran_type = '391' AND TL.description = 'Unload/Unpick (pick)' AND NOT EXISTS (SELECT 1 FROM t_tran_log TL2 WHERE TL2.control_number = TL.control_number AND TL2.wh_id = TL.wh_id AND TL2.item_number = TL.item_number AND TL2.tran_type = '301' AND TL2.description = 'Picking (pick)' AND (CAST(TL2.start_tran_date AS DATETIME) + CAST(TL2.start_tran_time AS DATETIME) > CAST(TL.start_tran_date AS DATETIME) + CAST(TL.start_tran_time AS DATETIME))) AND (ISNULL(PD.picked_quantity, 0) <> 0 OR ISNULL(PD.staged_quantity, 0) <> 0 OR PD.status <> 'RELEASED' OR SI.type <> 'STORAGE' OR SI.location_id <> (SELECT TOP 1 TL_PICK.location_id FROM t_tran_log TL_PICK WHERE TL_PICK.control_number = TL.control_number AND TL_PICK.wh_id = TL.wh_id AND TL_PICK.item_number = TL.item_number AND TL_PICK.tran_type = '301' ORDER BY TL_PICK.start_tran_date DESC, TL_PICK.start_tran_time DESC) OR HM.control_number IS NOT NULL OR HM.type <> 'IV' OR HD.storage_type IS NOT NULL OR WQ.work_status <> 'U')"
  find_picked_qty: "SELECT picked_quantity FROM t_pick_detail WHERE wh_id = :w AND order_number = :o AND item_number = :i"
  check_columns: "SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('t_pick_detail') AND name = 'pick_location'"
  find_pick_loc: "SELECT pick_location FROM t_pick_detail WHERE wh_id = :w AND order_number = :o AND item_number = :i"
  find_tran_loc_301: "SELECT TOP 1 location_id FROM t_tran_log WHERE wh_id = :w AND tran_type = '301' AND item_number = :i AND control_number = :o ORDER BY start_tran_date DESC, start_tran_time DESC"
  find_tran_source_loc_301: "SELECT TOP 1 source_location_id FROM t_tran_log WHERE wh_id = :w AND tran_type = '301' AND item_number = :i AND control_number = :o ORDER BY start_tran_date DESC, start_tran_time DESC"
  update_pick_detail: "UPDATE t_pick_detail SET staged_quantity = CASE WHEN staged_quantity >= :q THEN staged_quantity - :q ELSE 0 END, picked_quantity = picked_quantity - :q, status = CASE WHEN picked_quantity - :q > 0 THEN 'PICKED' ELSE 'RELEASED' END WHERE wh_id = :w AND order_number = :o AND item_number = :i"
  check_item_indicator: "SELECT item_hu_indicator FROM t_location WHERE wh_id = :w AND location_id = :l"
  check_stored_item: "SELECT 1 FROM t_stored_item WHERE wh_id = :w AND item_number = :i AND type = 'STORAGE' AND location_id = :l"
  update_stored_qty_add: "UPDATE t_stored_item SET actual_qty = actual_qty + :q WHERE wh_id = :w AND item_number = :i AND type = 'STORAGE' AND location_id = :l"
  update_stored_qty_move: "UPDATE t_stored_item SET type = 'STORAGE', location_id = :l, actual_qty = :q WHERE wh_id = :w AND item_number = :i AND type = :o"
  update_stored_qty_sub: "UPDATE t_stored_item SET actual_qty = actual_qty - :q WHERE wh_id = :w AND item_number = :i AND type = :o"
  delete_empty_stored_item: "DELETE FROM t_stored_item WHERE wh_id = :w AND item_number = :i AND type = :o AND actual_qty <= 0"
  find_hu_id: "SELECT TOP 1 hu_id FROM t_hu_detail WHERE wh_id = :w AND item_number = :i AND storage_type = :o"
  update_hu_qty_sub: "UPDATE t_hu_detail SET actual_qty = actual_qty - :q WHERE wh_id = :w AND hu_id = :h AND item_number = :i AND storage_type = :o"
  delete_empty_hu_detail: "DELETE FROM t_hu_detail WHERE wh_id = :w AND hu_id = :h AND item_number = :i AND storage_type = :o AND actual_qty <= 0"
  check_hu_detail: "SELECT 1 FROM t_hu_detail WHERE wh_id = :w AND hu_id = :h"
  delete_empty_hu_master: "DELETE FROM t_hu_master WHERE wh_id = :w AND hu_id = :h"
  update_work_q: "UPDATE t_work_q SET work_status = CASE WHEN EXISTS (SELECT 1 FROM t_pick_detail pd WHERE pd.work_q_id = t_work_q.work_q_id AND pd.picked_quantity >= pd.planned_quantity) THEN 'C' ELSE 'U' END WHERE wh_id = :w AND pick_ref_number = :o AND work_q_id IN (SELECT work_q_id FROM t_pick_detail WHERE order_number = :o AND wh_id = :w AND item_number = :i)"
  manual_unpick_update_pick: "UPDATE t_pick_detail SET staged_quantity = 0, picked_quantity = 0, status = 'RELEASED' WHERE order_number = :o AND wh_id = :w AND item_number = :i"
  manual_update_stored_qty_add: "UPDATE S SET S.actual_qty = S.actual_qty + O.actual_qty FROM t_stored_item S JOIN t_stored_item O ON O.wh_id = S.wh_id AND O.item_number = S.item_number WHERE S.wh_id = :w AND S.item_number = :i AND S.type = 'STORAGE' AND O.type = :o"
  manual_delete_stored_item: "DELETE FROM t_stored_item WHERE wh_id = :w AND item_number = :i AND type = :o"
  manual_update_stored_item_move: "UPDATE t_stored_item SET type = 'STORAGE', location_id = :l WHERE wh_id = :w AND item_number = :i AND type = :o"
  manual_delete_hu_detail: "DELETE FROM t_hu_detail WHERE wh_id = :w AND hu_id = :h AND item_number = :i AND storage_type = :o"
  manual_get_distinct_item_count: "SELECT COUNT(DISTINCT item_number) FROM t_hu_detail WHERE wh_id = :w AND hu_id = :h"
  manual_insert_hu_master: "INSERT INTO t_hu_master (hu_id, type, control_number, location_id, status, wh_id) VALUES (:h, 'LP', NULL, :l, 'A', :w)"
  manual_update_hu_detail_multi: "UPDATE t_hu_detail SET hu_id = :nh, storage_type = 'STORAGE', location_id = :l WHERE wh_id = :w AND hu_id = :oh AND item_number = :i"
  manual_update_work_q: "UPDATE t_work_q SET work_status = 'U' WHERE wh_id = :w AND pick_ref_number = :o AND work_q_id IN (SELECT work_q_id FROM t_pick_detail WHERE order_number = :o AND wh_id = :w AND item_number = :i)"
"""

    # Ensure agents table exists
    agents_exists = conn.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'agents'"
    ).fetchone()[0] > 0
    if not agents_exists:
        conn.execute("""
            CREATE TABLE agents (
                id          NVARCHAR(50) PRIMARY KEY,
                name        NVARCHAR(100) NOT NULL,
                description NVARCHAR(255),
                flow_yaml   NVARCHAR(MAX)
            )
        """)
        conn.commit()
        logger.info("Created agents table.")

    # Populate default agents if empty
    if conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO agents (id, name, description, flow_yaml) VALUES (?, ?, ?, ?)",
            ("device_reset", "Device Reset", "Auto-scans and resets stuck devices on the warehouse floor", device_reset_yaml)
        )
        conn.execute(
            "INSERT INTO agents (id, name, description, flow_yaml) VALUES (?, ?, ?, ?)",
            ("unpick", "Unpick Agent", "Manages manual, partial, and automated inventory unpicks", unpick_yaml)
        )
        conn.commit()
        logger.info("Pre-populated agents table with default configurations.")
    else:
        # Check if existing agents need update (migration)
        for agent_id, default_yaml in [("device_reset", device_reset_yaml), ("unpick", unpick_yaml)]:
            row = conn.execute("SELECT flow_yaml FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row:
                current_yaml = row[0] or ""
                if agent_id == "unpick" and "manual_" not in current_yaml:
                    conn.execute("UPDATE agents SET flow_yaml = ? WHERE id = ?", (default_yaml, agent_id))
                    logger.info("Updated flow_yaml for %s to include manual unpick queries.", agent_id)
                elif agent_id == "device_reset" and "find_employee_by_id" not in current_yaml:
                    conn.execute("UPDATE agents SET flow_yaml = ? WHERE id = ?", (default_yaml, agent_id))
                    logger.info("Updated flow_yaml for %s to include employee ID search queries.", agent_id)
        conn.commit()

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


def load_agent_queries(agent_id, default_queries):
    try:
        conn = _get_conn()
        row = conn.execute("SELECT flow_yaml FROM agents WHERE id = ?", (agent_id,)).fetchone()
        conn.close()
        if row and row[0]:
            data = yaml.safe_load(row[0]) or {}
            queries = data.get("queries", {})
            merged = dict(default_queries)
            for k, v in queries.items():
                if v is not None:
                    merged[k] = v
            return merged
    except Exception as e:
        logger.warning("Could not load dynamic queries for agent %s: %s", agent_id, e)
    return default_queries


def execute_dynamic_query(conn_or_cursor, query, params):
    """
    Executes a query dynamically.
    If conn_or_cursor is a pyodbc Cursor, converts named params (:name) to ? and executes.
    If conn_or_cursor is a SQLAlchemy Connection, wraps query in text() and executes.
    """
    type_str = str(type(conn_or_cursor))
    if "cursor" in type_str.lower():
        # Convert named parameters (:param) to ?
        pattern = r':([a-zA-Z0-9_]+)'
        keys = re.findall(pattern, query)
        q_converted = re.sub(pattern, '?', query)
        ordered_values = [params.get(k) for k in keys]
        return conn_or_cursor.execute(q_converted, ordered_values)
    else:
        # SQLAlchemy Connection
        from sqlalchemy import text as _text
        return conn_or_cursor.execute(_text(query), params)


@bp.route("/api/v0/agents", methods=["GET"])
@login_required
def list_agents_public():
    conn = _get_conn()
    rows = conn.execute("SELECT id, name, description, flow_yaml FROM agents ORDER BY id").fetchall()
    conn.close()
    return jsonify({"agents": [
        {"id": r[0], "name": r[1], "description": r[2], "flow_yaml": r[3]}
        for r in rows
    ]})


@bp.route("/api/v0/admin/agents", methods=["POST"])
def create_agent():
    from auth import superadmin_required_check
    err = superadmin_required_check()
    if err: return err

    data = request.get_json() or {}
    agent_id = data.get("id", "").strip().lower()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    flow_yaml = data.get("flow_yaml", "").strip()

    if not agent_id or not name:
        return jsonify({"type": "error", "error": "id and name are required"}), 400
    if not re.match(r'^[a-z0-9_]{3,50}$', agent_id):
        return jsonify({"type": "error", "error": "ID must be 3-50 alphanumeric characters or underscores"}), 400

    if flow_yaml:
        try:
            yaml.safe_load(flow_yaml)
        except Exception as e:
            return jsonify({"type": "error", "error": f"Invalid YAML syntax: {e}"}), 400

    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO agents (id, name, description, flow_yaml) VALUES (?, ?, ?, ?)",
            (agent_id, name, description, flow_yaml)
        )
        conn.commit()
        log_audit_action(current_user.username, "CREATE_AGENT", agent_id, {"name": name, "description": description})
    except Exception as e:
        conn.close()
        if "UNIQUE" in str(e).upper() or "PRIMARY KEY" in str(e).upper():
            return jsonify({"type": "error", "error": "Agent ID already exists"}), 409
        raise
    conn.close()
    return jsonify({"type": "success"}), 201


@bp.route("/api/v0/admin/agents/<string:agent_id>", methods=["PATCH"])
def update_agent(agent_id):
    from auth import superadmin_required_check
    err = superadmin_required_check()
    if err: return err

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    flow_yaml = data.get("flow_yaml")

    conn = _get_conn()
    row = conn.execute("SELECT id, name, description FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"type": "error", "error": "Agent not found"}), 404

    if flow_yaml is not None:
        flow_yaml = flow_yaml.strip()
        try:
            yaml.safe_load(flow_yaml)
        except Exception as e:
            conn.close()
            return jsonify({"type": "error", "error": f"Invalid YAML syntax: {e}"}), 400

    updates, params = [], []
    if name:
        updates.append("name = ?"); params.append(name)
    if description:
        updates.append("description = ?"); params.append(description)
    if flow_yaml is not None:
        updates.append("flow_yaml = ?"); params.append(flow_yaml)

    if updates:
        params.append(agent_id)
        conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        log_audit_action(current_user.username, "UPDATE_AGENT", agent_id, {"name": name, "description": description})
    conn.close()
    return jsonify({"type": "success"})


@bp.route("/api/v0/admin/agents/<string:agent_id>", methods=["DELETE"])
def delete_agent(agent_id):
    from auth import superadmin_required_check
    err = superadmin_required_check()
    if err: return err

    if agent_id in ("device_reset", "unpick"):
        return jsonify({"type": "error", "error": "Cannot delete core system agents"}), 400

    conn = _get_conn()
    row = conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"type": "error", "error": "Agent not found"}), 404

    conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    conn.commit()
    conn.close()

    log_audit_action(current_user.username, "DELETE_AGENT", agent_id, {})
    return jsonify({"type": "success"})


@bp.route("/api/v0/auth/my_history", methods=["GET"])
@login_required
def get_my_history():
    conn = _get_conn()
    cursor = conn.execute(
        "SELECT id, username, timestamp, action, target, details FROM audit_logs WHERE username = ? ORDER BY id DESC",
        (current_user.username,)
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


