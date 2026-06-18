import os
import json
import logging
from datetime import datetime

import bcrypt
from flask import Blueprint, request, jsonify
from flask_login import current_user

from auth import admin_required, _get_conn, KNOWN_AGENT_PERMS, VALID_ROLES

logger = logging.getLogger(__name__)
bp = Blueprint("users", __name__)


@bp.route("/api/v0/admin/users", methods=["GET"])
@admin_required
def list_users():
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, username, role, agent_perms, is_active, created_at, last_login, display_name FROM users"
        " ORDER BY id"
    ).fetchall()
    conn.close()
    return jsonify({"users": [
        {
            "id":           r[0],
            "username":     r[1],
            "display_name": r[7] or r[1],
            "role":         r[2],
            "agent_perms":  json.loads(r[3] or "[]"),
            "is_active":    bool(r[4]),
            "created_at":   r[5],
            "last_login":   r[6],
        }
        for r in rows
    ]})


@bp.route("/api/v0/admin/users", methods=["POST"])
@admin_required
def create_user():
    data         = request.get_json() or {}
    username     = data.get("username", "").strip()
    password     = data.get("password", "")
    display_name = data.get("display_name", "").strip() or username
    role         = data.get("role", "user")
    agent_perms  = data.get("agent_perms", [])

    if not username or not password:
        return jsonify({"type": "error", "error": "username and password required"}), 400
    if role in ("admin", "superadmin") and not current_user.is_superadmin():
        return jsonify({"type": "error", "error": "Only superadmin can manage admin/superadmin accounts"}), 403
    if role not in VALID_ROLES:
        return jsonify({"type": "error", "error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400

    rounds  = int(os.getenv("BCRYPT_ROUNDS", "12"))
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=rounds)).decode()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, agent_perms, is_active, created_at, display_name, force_change_password)"
            " VALUES (?, ?, ?, ?, 1, ?, ?, 1)",
            (username, pw_hash, role, json.dumps(agent_perms), datetime.utcnow().isoformat(), display_name),
        )
        conn.commit()
        from auth import log_audit_action
        log_audit_action(
            username=current_user.username,
            action="CREATE_USER",
            target=username,
            details={"fields": {"username": username, "role": role, "display_name": display_name, "agent_perms": agent_perms}}
        )
    except Exception as e:
        conn.close()
        if "UNIQUE" in str(e).upper():
            return jsonify({"type": "error", "error": "Username already exists"}), 409
        raise
    conn.close()
    return jsonify({"type": "success"}), 201


@bp.route("/api/v0/admin/users/<int:user_id>", methods=["PATCH"])
@admin_required
def update_user(user_id):
    data = request.get_json() or {}
    conn = _get_conn()
    if not conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
        conn.close()
        return jsonify({"type": "error", "error": "User not found"}), 404

    # Fetch before state
    row_before = conn.execute("SELECT username, role, agent_perms, is_active, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    target_role = row_before[1]
    if target_role in ("admin", "superadmin") and not current_user.is_superadmin():
        conn.close()
        return jsonify({"type": "error", "error": "Only superadmin can manage admin/superadmin accounts"}), 403

    updates, params = [], []

    if "display_name" in data:
        dn = data["display_name"].strip()
        if dn:
            updates.append("display_name = ?"); params.append(dn)

    if "role" in data:
        new_role = data["role"]
        if new_role in ("admin", "superadmin") and not current_user.is_superadmin():
            conn.close()
            return jsonify({"type": "error", "error": "Only superadmin can assign admin/superadmin roles"}), 403
        if new_role not in VALID_ROLES:
            conn.close()
            return jsonify({"type": "error", "error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400
        updates.append("role = ?"); params.append(new_role)

    if "agent_perms" in data:
        updates.append("agent_perms = ?"); params.append(json.dumps(data["agent_perms"]))

    if "is_active" in data:
        if not data["is_active"] and user_id == current_user.id:
            conn.close()
            return jsonify({"type": "error", "error": "Cannot deactivate your own account"}), 400
        updates.append("is_active = ?"); params.append(1 if data["is_active"] else 0)

    if data.get("password"):
        rounds  = int(os.getenv("BCRYPT_ROUNDS", "12"))
        pw_hash = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt(rounds=rounds)).decode()
        updates.append("password_hash = ?"); params.append(pw_hash)

    if updates:
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()

        row_after = conn.execute("SELECT username, role, agent_perms, is_active, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
        before_dict = {"username": row_before[0], "role": row_before[1], "agent_perms": json.loads(row_before[2] or "[]"), "is_active": bool(row_before[3]), "display_name": row_before[4]}
        after_dict = {"username": row_after[0], "role": row_after[1], "agent_perms": json.loads(row_after[2] or "[]"), "is_active": bool(row_after[3]), "display_name": row_after[4]}

        from auth import log_audit_action
        log_audit_action(
            username=current_user.username,
            action="UPDATE_USER",
            target=str(user_id),
            details={"before": before_dict, "after": after_dict}
        )

    conn.close()
    return jsonify({"type": "success"})


@bp.route("/api/v0/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def deactivate_user(user_id):
    if user_id == current_user.id:
        return jsonify({"type": "error", "error": "Cannot deactivate your own account"}), 400
    conn = _get_conn()
    row = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"type": "error", "error": "User not found"}), 404
    username_target, target_role = row[0], row[1]
    if target_role in ("admin", "superadmin") and not current_user.is_superadmin():
        conn.close()
        return jsonify({"type": "error", "error": "Only superadmin can manage admin/superadmin accounts"}), 403
    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    conn.commit()
    from auth import log_audit_action
    log_audit_action(
        username=current_user.username,
        action="DEACTIVATE_USER",
        target=str(user_id),
        details={"fields": {"username": username_target, "is_active": False}}
    )
    conn.close()
    return jsonify({"type": "success"})


@bp.route("/api/v0/admin/users/<int:user_id>/perms", methods=["GET"])
@admin_required
def get_user_perms(user_id):
    conn = _get_conn()
    row = conn.execute("SELECT agent_perms, role FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"type": "error", "error": "User not found"}), 404
    target_role = row[1]
    if target_role in ("admin", "superadmin") and not current_user.is_superadmin():
        return jsonify({"type": "error", "error": "Only superadmin can manage admin/superadmin accounts"}), 403
    return jsonify({"perms": json.loads(row[0] or "[]")})


@bp.route("/api/v0/admin/users/<int:user_id>/perms", methods=["POST"])
@admin_required
def set_user_perms(user_id):
    data  = request.get_json() or {}
    perms = data.get("perms", [])
    conn  = _get_conn()
    row_before = conn.execute("SELECT username, agent_perms, role FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row_before:
        conn.close()
        return jsonify({"type": "error", "error": "User not found"}), 404
    username_target, before_perms, target_role = row_before[0], json.loads(row_before[1] or "[]"), row_before[2]
    if target_role in ("admin", "superadmin") and not current_user.is_superadmin():
        conn.close()
        return jsonify({"type": "error", "error": "Only superadmin can manage admin/superadmin accounts"}), 403

    conn.execute("UPDATE users SET agent_perms = ? WHERE id = ?", (json.dumps(perms), user_id))
    conn.commit()
    from auth import log_audit_action
    log_audit_action(
        username=current_user.username,
        action="UPDATE_USER_PERMS",
        target=str(user_id),
        details={
            "before": {"username": username_target, "agent_perms": before_perms},
            "after": {"username": username_target, "agent_perms": perms}
        }
    )
    conn.close()
    return jsonify({"type": "success"})
