import os
import re
import json
import logging

from flask import Blueprint, request, jsonify
from flask_login import login_required

logger = logging.getLogger(__name__)
bp = Blueprint("db_config", __name__)

# Names must start with alphanumeric; allow letters, digits, spaces, hyphens, underscores, dots
_VALID_NAME_RE = re.compile(r'^[A-Za-z0-9][\w\s\-\.]{0,62}$')


def _config_path():
    data_dir = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "db_configs.json")


import base64
from cryptography.fernet import Fernet

_fernet = None

def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
        
    key_str = os.getenv("DB_ENCRYPTION_KEY")
    if not key_str:
        is_debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
        if is_debug:
            # Generate a stable fallback key for local debugging/development (32 bytes base64 encoded)
            fallback_bytes = b"dev-encryption-key-fallback-32-b"
            key_str = base64.urlsafe_b64encode(fallback_bytes).decode()
            logger.warning(
                "DB_ENCRYPTION_KEY env var not found. "
                "Using default fallback key for local development. "
                "DO NOT USE IN PRODUCTION!"
            )
        else:
            raise AssertionError(
                "Production Startup Error: DB_ENCRYPTION_KEY env var is not set, "
                "but FLASK_DEBUG is false. Passwords must be encrypted in production!"
            )
            
    try:
        key_bytes = key_str.encode()
        _fernet = Fernet(key_bytes)
        return _fernet
    except Exception as e:
        raise AssertionError(f"Invalid DB_ENCRYPTION_KEY: {e}")


def _encrypt_password(password: str) -> str:
    if not password:
        return ""
    fernet = _get_fernet()
    encrypted_bytes = fernet.encrypt(password.encode())
    return "enc:" + encrypted_bytes.decode()


def _decrypt_password(enc_password: str) -> str:
    if not enc_password:
        return ""
    if not enc_password.startswith("enc:"):
        return enc_password
    fernet = _get_fernet()
    try:
        encrypted_bytes = enc_password[4:].encode()
        decrypted_bytes = fernet.decrypt(encrypted_bytes)
        return decrypted_bytes.decode()
    except Exception as e:
        logger.error("Failed to decrypt database password: %s", e)
        return ""


def load_configs() -> dict:
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                configs = json.load(f)
                for name, entry in configs.items():
                    if "db" in entry and "password" in entry["db"]:
                        entry["db"]["password"] = _decrypt_password(entry["db"]["password"])
                    if "log_db" in entry and entry["log_db"] and "password" in entry["log_db"]:
                        entry["log_db"]["password"] = _decrypt_password(entry["log_db"]["password"])
                return configs
        except Exception as e:
            logger.warning("Could not load db_configs.json: %s", e)
    return {}


def save_configs(configs: dict):
    import copy
    configs_copy = copy.deepcopy(configs)
    for name, entry in configs_copy.items():
        if "db" in entry and "password" in entry["db"]:
            entry["db"]["password"] = _encrypt_password(entry["db"]["password"])
        if "log_db" in entry and entry["log_db"] and "password" in entry["log_db"]:
            entry["log_db"]["password"] = _encrypt_password(entry["log_db"]["password"])
    with open(_config_path(), "w") as f:
        json.dump(configs_copy, f, indent=2)


def get_config(db_config_id: str):
    return load_configs().get(db_config_id)


def _safe_config(db_config_id, entry):
    """Return config entry with password masked."""
    safe = dict(entry)
    db = dict(safe.get("db", {}))
    db["password"] = "***" if db.get("password") else ""
    safe["db"] = db
    if "log_db" in safe and safe["log_db"]:
        log_db = dict(safe["log_db"])
        log_db["password"] = "***" if log_db.get("password") else ""
        safe["log_db"] = log_db
    safe["id"]   = db_config_id
    safe["name"] = db_config_id  # name IS the key / ID
    return safe


# ── Non-admin: name list for agent dropdowns ─────────────────────────────────

@bp.route("/api/v0/db_configs", methods=["GET"])
@login_required
def list_db_configs_user():
    configs = load_configs()
    return jsonify({"db_configs": [{"id": k, "name": k} for k in configs]})


# ── Admin CRUD ────────────────────────────────────────────────────────────────

@bp.route("/api/v0/admin/db_configs", methods=["GET"])
def list_db_configs():
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err
    configs = load_configs()
    return jsonify({"db_configs": {k: _safe_config(k, v) for k, v in configs.items()}})


@bp.route("/api/v0/admin/db_configs", methods=["POST"])
def create_db_config():
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err

    data    = request.get_json() or {}
    name    = data.get("name", "").strip()
    db_type = data.get("db_type", "mssql").lower()

    if not name:
        return jsonify({"type": "error", "error": "name is required"}), 400
    if not _VALID_NAME_RE.match(name):
        return jsonify({"type": "error", "error": "Name must start with alphanumeric; letters, digits, spaces, hyphens, dots allowed (max 63 chars)"}), 400
    if db_type not in ("mssql", "oracle"):
        return jsonify({"type": "error", "error": "db_type must be 'mssql' or 'oracle'"}), 400

    configs = load_configs()
    if name in configs:
        return jsonify({"type": "error", "error": "A config with this name already exists"}), 409

    configs[name] = {
        "db_type": db_type,
        "db":      data.get("db", {}),
        "notify":  data.get("notify", {}),
    }
    if "log_db" in data and data["log_db"]:
        configs[name]["log_db"] = data["log_db"]
    save_configs(configs)

    from auth import log_audit_action
    from flask_login import current_user
    safe_entry = _safe_config(name, configs[name])
    log_audit_action(
        username=current_user.username,
        action="CREATE_DB_CONFIG",
        target=name,
        details={"fields": safe_entry}
    )

    return jsonify({"type": "success", "id": name}), 201


@bp.route("/api/v0/admin/db_configs/<string:db_config_id>", methods=["PATCH"])
def update_db_config(db_config_id):
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err

    configs = load_configs()
    if db_config_id not in configs:
        return jsonify({"type": "error", "error": "Not found"}), 404

    before_safe = _safe_config(db_config_id, configs[db_config_id])

    data     = request.get_json() or {}
    entry    = configs[db_config_id]
    final_id = db_config_id

    # Handle rename
    new_name = data.get("name", "").strip()
    if new_name and new_name != db_config_id:
        if not _VALID_NAME_RE.match(new_name):
            return jsonify({"type": "error", "error": "Invalid name format"}), 400
        if new_name in configs:
            return jsonify({"type": "error", "error": "A config with this name already exists"}), 409
        del configs[db_config_id]
        from db import invalidate_engine
        invalidate_engine(db_config_id)
        final_id = new_name
        configs[final_id] = entry

    if "db_type" in data:
        if data["db_type"] not in ("mssql", "oracle"):
            return jsonify({"type": "error", "error": "db_type must be 'mssql' or 'oracle'"}), 400
        entry["db_type"] = data["db_type"]

    if "db" in data:
        db_patch = dict(data["db"])
        if db_patch.get("password") in ("***", "", None):
            db_patch.pop("password", None)  # preserve existing password
        entry.setdefault("db", {}).update(db_patch)

    if "log_db" in data:
        if data["log_db"] is None:
            entry.pop("log_db", None)
        else:
            log_db_patch = dict(data["log_db"])
            if log_db_patch.get("password") in ("***", "", None):
                log_db_patch.pop("password", None)
            entry.setdefault("log_db", {}).update(log_db_patch)

    if "notify" in data:
        entry.setdefault("notify", {}).update(data["notify"])

    save_configs(configs)
    from db import invalidate_engine
    invalidate_engine(db_config_id)
    if final_id != db_config_id:
        invalidate_engine(final_id)

    from auth import log_audit_action
    from flask_login import current_user
    after_safe = _safe_config(final_id, configs[final_id])
    log_audit_action(
        username=current_user.username,
        action="UPDATE_DB_CONFIG",
        target=db_config_id,
        details={"before": before_safe, "after": after_safe}
    )

    return jsonify({"type": "success", "id": final_id})


@bp.route("/api/v0/admin/db_configs/<string:db_config_id>", methods=["DELETE"])
def delete_db_config(db_config_id):
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err

    configs = load_configs()
    before_safe = _safe_config(db_config_id, configs[db_config_id]) if db_config_id in configs else {}
    configs.pop(db_config_id, None)
    save_configs(configs)
    from db import invalidate_engine
    invalidate_engine(db_config_id)

    from auth import log_audit_action
    from flask_login import current_user
    log_audit_action(
        username=current_user.username,
        action="DELETE_DB_CONFIG",
        target=db_config_id,
        details={"fields": before_safe}
    )

    return jsonify({"type": "success"})


@bp.route("/api/v0/admin/db_configs/<string:db_config_id>/test_connection", methods=["POST"])
def test_db_connection(db_config_id):
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err

    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "Config not found"}), 404
    try:
        from db import pyodbc_connect
        conn = pyodbc_connect(cfg)
        conn.close()
        return jsonify({"type": "success", "message": "Connection successful"})
    except Exception as e:
        return jsonify({"type": "error", "error": str(e)}), 500


@bp.route("/api/v0/admin/db_configs/test_connection", methods=["POST"])
def test_unsaved_db_connection():
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    db_type = data.get("db_type", "mssql").lower()
    target = data.get("target", "primary")
    db_info = data.get("db", {}) if target == "primary" else data.get("log_db", {})

    cfg = {
        "db_type": db_type,
        "db": dict(db_info)
    }

    # If password is *** or empty, lookup existing config if name matches
    password = cfg["db"].get("password", "")
    if password in ("***", "", None):
        if name:
            existing_cfg = get_config(name)
            if existing_cfg:
                if target == "primary" and "db" in existing_cfg:
                    cfg["db"]["password"] = existing_cfg["db"].get("password", "")
                elif target == "log" and "log_db" in existing_cfg:
                    cfg["db"]["password"] = existing_cfg["log_db"].get("password", "")
                else:
                    cfg["db"]["password"] = ""
            else:
                cfg["db"]["password"] = ""
        else:
            cfg["db"]["password"] = ""

    try:
        from db import pyodbc_connect
        conn = pyodbc_connect(cfg)
        conn.close()
        return jsonify({"type": "success", "message": "Connection successful"})
    except Exception as e:
        return jsonify({"type": "error", "error": str(e)}), 500


@bp.route("/api/v0/notify/test", methods=["POST"])
def test_notification():
    from auth import admin_required_check
    err = admin_required_check()
    if err: return err

    data         = request.get_json() or {}
    db_config_id = data.get("db_config_id", "")
    channel      = data.get("channel", "teams")
    cfg          = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "Config not found"}), 404
    from notify import send_test
    ok = send_test(cfg, channel)
    if ok:
        return jsonify({"type": "success"})
    return jsonify({"type": "error", "error": "Failed — check webhook URL and try again"}), 500
