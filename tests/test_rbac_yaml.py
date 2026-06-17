import pytest
import json
import re
from unittest.mock import MagicMock, patch
from flask import Flask, jsonify
from flask_login import LoginManager
from auth import bp as auth_bp, execute_dynamic_query, User
from users import bp as users_bp
from sqlalchemy import text

class MyCursor(MagicMock):
    pass

@pytest.fixture
def app():
    app = Flask("test_rbac_yaml_app")
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
    app.register_blueprint(users_bp)
    return app

@pytest.fixture
def client(app):
    return app.test_client()

def test_execute_dynamic_query_pyodbc_cursor():
    cursor = MyCursor()
    cursor.execute.return_value = cursor
    query = "SELECT * FROM t WHERE wh_id = :w AND order_number = :o"
    params = {"w": "WH1", "o": "ORD1"}
    
    # Run helper
    res = execute_dynamic_query(cursor, query, params)
    
    assert res == cursor
    cursor.execute.assert_called_once()
    called_sql, called_params = cursor.execute.call_args[0]
    assert called_sql == "SELECT * FROM t WHERE wh_id = ? AND order_number = ?"
    assert called_params == ["WH1", "ORD1"]

def test_execute_dynamic_query_sqlalchemy_conn():
    conn = MagicMock()
    query = "SELECT * FROM t WHERE wh_id = :w"
    params = {"w": "WH1"}
    
    execute_dynamic_query(conn, query, params)
    
    conn.execute.assert_called_once()
    called_sql_obj, called_params = conn.execute.call_args[0][0], conn.execute.call_args[0][1]
    assert called_params == {"w": "WH1"}

@patch("auth._get_conn")
def test_get_agents_public(mock_get_conn, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.force_change_password = False
    
    app.config["test_users"] = {"1": user}
    
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        
    conn = MagicMock()
    mock_get_conn.return_value = conn
    cursor = MagicMock()
    conn.execute.return_value = cursor
    cursor.fetchall.return_value = [
        ("device_reset", "Device Reset", "Relocation", "queries:\n  check: SELECT 1")
    ]
    
    res = client.get("/api/v0/agents")
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["agents"]) == 1
    assert data["agents"][0]["id"] == "device_reset"

@patch("auth._get_conn")
def test_superadmin_agent_crud_success(mock_get_conn, app, client):
    # Logged in as superadmin
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.is_superadmin.return_value = True
    user.force_change_password = False
    
    app.config["test_users"] = {"1": user}
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        
    conn = MagicMock()
    mock_get_conn.return_value = conn
    
    # 1. Create agent (POST)
    res = client.post("/api/v0/admin/agents", json={
        "id": "custom_agent",
        "name": "Custom",
        "description": "Desc",
        "flow_yaml": "queries:\n  check: SELECT 2"
    })
    assert res.status_code == 201
    
    # 2. Patch agent (PATCH)
    conn.execute.return_value.fetchone.return_value = ("custom_agent", "Custom", "Desc")
    res = client.patch("/api/v0/admin/agents/custom_agent", json={
        "name": "Custom Patched",
        "flow_yaml": "queries:\n  check: SELECT 3"
    })
    assert res.status_code == 200
    
    # 3. Delete agent (DELETE)
    res = client.delete("/api/v0/admin/agents/custom_agent")
    assert res.status_code == 200

@patch("auth._get_conn")
def test_admin_agent_crud_forbidden(mock_get_conn, app, client):
    # Logged in as admin (not superadmin)
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "2"
    user.is_superadmin.return_value = False
    user.force_change_password = False
    
    app.config["test_users"] = {"2": user}
    with client.session_transaction() as sess:
        sess["_user_id"] = "2"
        
    res = client.post("/api/v0/admin/agents", json={"id": "x", "name": "x"})
    assert res.status_code == 403

def test_yaml_syntax_validation(app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.is_superadmin.return_value = True
    user.force_change_password = False
    
    app.config["test_users"] = {"1": user}
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        
    res = client.post("/api/v0/admin/agents", json={
        "id": "custom_agent",
        "name": "Custom",
        "flow_yaml": "queries:\n  check: SELECT 2\n  unbalanced: : {"
    })
    assert res.status_code == 400
    assert "Invalid YAML syntax" in res.get_json()["error"]

@patch("users._get_conn")
def test_admin_blocked_from_editing_admin_roles(mock_get_conn, app, client):
    # Logged in as standard admin
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "2"
    user.is_superadmin.return_value = False
    user.is_admin.return_value = True
    user.force_change_password = False
    
    app.config["test_users"] = {"2": user}
    with client.session_transaction() as sess:
        sess["_user_id"] = "2"
        
    # Target is another admin (role='admin')
    conn = MagicMock()
    mock_get_conn.return_value = conn
    
    # Mock row lookup for user 3 (the target user we are editing)
    # SELECT username, role, agent_perms, is_active, display_name FROM users WHERE id = ?
    conn.execute.return_value.fetchone.return_value = ("other_admin", "admin", "[]", 1, "Other")
    
    # Try to edit target admin user
    res = client.patch("/api/v0/admin/users/3", json={
        "display_name": "Changed Name"
    })
    assert res.status_code == 403
    assert "Only superadmin can manage admin/superadmin accounts" in res.get_json()["error"]
