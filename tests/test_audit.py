import pytest
import json
from unittest.mock import MagicMock, patch
from flask import Flask, jsonify
from flask_login import LoginManager
from auth import bp as auth_bp, log_audit_action

@pytest.fixture
def app():
    app = Flask("test_app")
    app.secret_key = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = False

    login_manager = LoginManager()
    login_manager.init_app(app)

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify({"type": "error", "error": "Not authenticated"}), 401

    @login_manager.user_loader
    def load_user(user_id):
        return app.config.get("test_users", {}).get(user_id)

    app.register_blueprint(auth_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_get_audit_logs_unauthorized(client):
    res = client.get("/api/v0/admin/audit_logs")
    assert res.status_code == 401


def test_get_audit_logs_non_admin(app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "2"
    user.is_admin.return_value = False
    user.force_change_password = False

    app.config["test_users"] = {"2": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "2"

    res = client.get("/api/v0/admin/audit_logs")
    assert res.status_code == 403


@patch("auth._get_conn")
def test_get_audit_logs_admin_success(mock_get_conn, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.is_admin.return_value = True
    user.force_change_password = False

    app.config["test_users"] = {"1": user}

    # Mock SQL connection and fetchall
    conn = MagicMock()
    mock_get_conn.return_value = conn
    cursor = MagicMock()
    conn.execute.return_value = cursor
    cursor.fetchall.return_value = [
        (1, "admin", "2026-06-16T12:00:00", "LOGIN_SUCCESS", "auth", '{"role": "admin", "ip_address": "127.0.0.1", "user_agent": "Mozilla"}')
    ]

    with client.session_transaction() as sess:
        sess["_user_id"] = "1"

    res = client.get("/api/v0/admin/audit_logs")
    print("RESPONSE BODY:", res.data)
    assert res.status_code == 200
    
    data = res.get_json()
    assert "audit_logs" in data
    assert len(data["audit_logs"]) == 1
    assert data["audit_logs"][0]["username"] == "admin"
    assert data["audit_logs"][0]["action"] == "LOGIN_SUCCESS"
    assert data["audit_logs"][0]["details"]["ip_address"] == "127.0.0.1"


@patch("auth._get_conn")
def test_download_audit_logs_csv(mock_get_conn, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.is_admin.return_value = True
    user.force_change_password = False

    app.config["test_users"] = {"1": user}

    conn = MagicMock()
    mock_get_conn.return_value = conn
    cursor = MagicMock()
    conn.execute.return_value = cursor
    cursor.fetchall.return_value = [
        (1, "admin", "2026-06-16T12:00:00", "LOGIN_SUCCESS", "auth", '{"role": "admin", "ip_address": "127.0.0.1", "user_agent": "Mozilla"}')
    ]

    with client.session_transaction() as sess:
        sess["_user_id"] = "1"

    res = client.get("/api/v0/admin/audit_logs/download?format=csv")
    assert res.status_code == 200
    assert res.headers["Content-Disposition"] == "attachment; filename=audit_logs.csv"
    assert "text/csv" in res.headers["Content-Type"]
    
    body = res.data.decode("utf-8")
    assert "ID,Username,Timestamp (UTC),Action,Target,IP Address,User Agent,Details" in body
    assert "1,admin,2026-06-16T12:00:00,LOGIN_SUCCESS,auth,127.0.0.1,Mozilla" in body


@patch("auth._get_conn")
def test_download_audit_logs_txt(mock_get_conn, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.is_admin.return_value = True
    user.force_change_password = False

    app.config["test_users"] = {"1": user}

    conn = MagicMock()
    mock_get_conn.return_value = conn
    cursor = MagicMock()
    conn.execute.return_value = cursor
    cursor.fetchall.return_value = [
        (1, "admin", "2026-06-16T12:00:00", "LOGIN_SUCCESS", "auth", '{"role": "admin", "ip_address": "127.0.0.1", "user_agent": "Mozilla"}')
    ]

    with client.session_transaction() as sess:
        sess["_user_id"] = "1"

    res = client.get("/api/v0/admin/audit_logs/download?format=txt")
    assert res.status_code == 200
    assert res.headers["Content-Disposition"] == "attachment; filename=audit_logs.txt"
    assert "text/plain" in res.headers["Content-Type"]
    
    body = res.data.decode("utf-8")
    assert "[2026-06-16T12:00:00] USER: admin | ACTION: LOGIN_SUCCESS | TARGET: auth | IP: 127.0.0.1" in body
    assert "UA: Mozilla" in body


@patch("auth._get_conn")
def test_log_audit_action_with_context(mock_get_conn, app):
    conn = MagicMock()
    mock_get_conn.return_value = conn

    with app.test_request_context(environ_overrides={
        "REMOTE_ADDR": "192.168.10.15",
        "HTTP_USER_AGENT": "Mozilla/5.0 (Test Browser)"
    }):
        log_audit_action("test_user", "ACTION_TEST", "target_obj", {"custom_val": True})

    conn.execute.assert_called_once()
    args = conn.execute.call_args[0]
    sql, params = args[0], args[1]
    
    assert "INSERT INTO audit_logs" in sql
    assert params[0] == "test_user"
    assert params[2] == "ACTION_TEST"
    assert params[3] == "target_obj"
    
    details = json.loads(params[4])
    assert details["custom_val"] is True
    assert details["ip_address"] == "192.168.10.15"
    assert details["user_agent"] == "Mozilla/5.0 (Test Browser)"
