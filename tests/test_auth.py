import pytest
from unittest.mock import MagicMock
from flask import Flask, jsonify
from flask_login import LoginManager
from auth import admin_required, superadmin_required, require_agent

@pytest.fixture
def app():
    app = Flask("test_app")
    app.secret_key = "test-secret"
    
    login_manager = LoginManager()
    login_manager.init_app(app)
    
    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify({"type": "error", "error": "Not authenticated"}), 401
        
    @login_manager.user_loader
    def load_user(user_id):
        return app.config.get("test_users", {}).get(user_id)
        
    @app.route("/admin")
    @admin_required
    def admin_route():
        return jsonify({"status": "admin_ok"})
        
    @app.route("/superadmin")
    @superadmin_required
    def superadmin_route():
        return jsonify({"status": "superadmin_ok"})
        
    @app.route("/agent-reset")
    @require_agent("device_reset")
    def agent_reset_route():
        return jsonify({"status": "reset_ok"})

    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_auth_blocked_when_anonymous(client):
    res = client.get("/admin")
    assert res.status_code == 401
    assert res.get_json()["error"] == "Not authenticated"


def test_admin_blocked_when_regular_user(app, client):
    # Mock user with role 'user'
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.is_admin.return_value = False
    
    app.config["test_users"] = {"1": user}
    
    with client.session_transaction() as sess:
        sess["_user_id"] = "1"
        
    res = client.get("/admin")
    assert res.status_code == 403
    assert res.get_json()["error"] == "Admin access required"


def test_admin_allowed_when_admin(app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "2"
    user.is_admin.return_value = True
    
    app.config["test_users"] = {"2": user}
    
    with client.session_transaction() as sess:
        sess["_user_id"] = "2"
        
    res = client.get("/admin")
    assert res.status_code == 200
    assert res.get_json()["status"] == "admin_ok"


def test_superadmin_blocked_when_admin(app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "3"
    user.is_superadmin.return_value = False
    
    app.config["test_users"] = {"3": user}
    
    with client.session_transaction() as sess:
        sess["_user_id"] = "3"
        
    res = client.get("/superadmin")
    assert res.status_code == 403
    assert res.get_json()["error"] == "Superadmin access required"


def test_superadmin_allowed_when_superadmin(app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "4"
    user.is_superadmin.return_value = True
    
    app.config["test_users"] = {"4": user}
    
    with client.session_transaction() as sess:
        sess["_user_id"] = "4"
        
    res = client.get("/superadmin")
    assert res.status_code == 200
    assert res.get_json()["status"] == "superadmin_ok"


def test_agent_permission_blocked_without_perm(app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "5"
    user.has_agent_perm.return_value = False
    
    app.config["test_users"] = {"5": user}
    
    with client.session_transaction() as sess:
        sess["_user_id"] = "5"
        
    res = client.get("/agent-reset")
    assert res.status_code == 403
    assert "Permission denied" in res.get_json()["error"]


def test_agent_permission_allowed_with_perm(app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "6"
    user.has_agent_perm.side_effect = lambda perm: perm == "device_reset"
    
    app.config["test_users"] = {"6": user}
    
    with client.session_transaction() as sess:
        sess["_user_id"] = "6"
        
    res = client.get("/agent-reset")
    assert res.status_code == 200
    assert res.get_json()["status"] == "reset_ok"
