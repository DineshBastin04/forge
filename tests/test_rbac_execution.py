import pytest
import json
from unittest.mock import MagicMock, patch
from flask import Flask, jsonify
from flask_login import LoginManager
from auth import bp as auth_bp, User
from db_config import bp as db_config_bp
from agents.device_reset import bp as dr_bp
from agents.unpick import bp as unpick_bp

@pytest.fixture
def app():
    app = Flask("test_rbac_exec_app")
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
    app.register_blueprint(db_config_bp)
    app.register_blueprint(dr_bp)
    app.register_blueprint(unpick_bp)
    return app

@pytest.fixture
def client(app):
    return app.test_client()

@patch("auth._get_conn")
@patch("agents.device_reset.get_config")
@patch("agents.device_reset.get_engine")
def test_device_reset_endpoints_success(mock_get_engine, mock_get_config, mock_auth_conn, app, client):
    # Standard user with device_reset permission
    user = User([6, "user_6", "hash", "user", '["device_reset"]', 1])
    app.config["test_users"] = {"6": user}
    with client.session_transaction() as sess:
        sess["_user_id"] = "6"

    # Mocks
    conn_mock = MagicMock()
    mock_auth_conn.return_value = conn_mock
    conn_mock.execute.return_value.fetchone.return_value = ("queries:\n  find_employee: SELECT 1\n  find_location: SELECT 1\n  check_stored_item: SELECT 1\n  check_hu_master: SELECT 1\n  find_staging: SELECT 1\n  update_employee: SELECT 1\n  update_location: SELECT 1",)
    
    mock_get_config.return_value = {"db_type": "mssql", "db": {}}
    mock_engine = MagicMock()
    mock_get_engine.return_value = mock_engine
    
    # 1. auto_scan
    mock_res = MagicMock()
    mock_res.fetchall.return_value = [("DEV001",)]
    mock_engine.connect.return_value.__enter__.return_value = mock_res
    mock_res.execute.return_value = mock_res
    
    res = client.post("/api/v0/device_reset_agent/auto_scan", json={"db_config_id": "test_db"})
    assert res.status_code == 200
    assert res.get_json()["type"] == "success"

    # 2. execute
    res = client.post("/api/v0/device_reset_agent/execute", json={"db_config_id": "test_db", "devices": [{"device_id": "DEV001"}]})
    assert res.status_code == 200
    assert res.get_json()["type"] == "success"

@patch("auth._get_conn")
@patch("agents.unpick.get_config")
@patch("agents.unpick.get_engine")
@patch("agents.unpick._do_manual_unpick_pyodbc")
def test_unpick_endpoints_success(mock_do_manual_unpick, mock_get_engine, mock_get_config, mock_auth_conn, app, client):
    # Standard user with unpick permission
    user = User([7, "user_7", "hash", "user", '["unpick"]', 1])
    app.config["test_users"] = {"7": user}
    with client.session_transaction() as sess:
        sess["_user_id"] = "7"

    # Mocks
    conn_mock = MagicMock()
    mock_auth_conn.return_value = conn_mock
    conn_mock.execute.return_value.fetchone.return_value = ("queries:\n  find_picked_qty: SELECT 1",)
    
    mock_get_config.return_value = {"db_type": "mssql", "db": {}}
    mock_engine = MagicMock()
    mock_get_engine.return_value = mock_engine
    
    # 1. auto_scan
    mock_res = MagicMock()
    mock_res.fetchall.return_value = [("ORD1", "WH1", "ITEM1")]
    mock_engine.connect.return_value.__enter__.return_value = mock_res
    mock_res.execute.return_value = mock_res
    
    res = client.post("/api/v0/unpick_agent/auto_scan", json={"db_config_id": "test_db"})
    assert res.status_code == 200
    assert res.get_json()["type"] == "success"

    # 2. manual_unpick
    mock_do_manual_unpick.return_value = {"status": "SUCCESS", "message": "Manual unpick successful"}
    res = client.post("/api/v0/unpick_agent/manual_unpick", json={
        "db_config_id": "test_db",
        "wh_id": "WH1",
        "order_number": "ORD1",
        "item_number": "ITEM1"
    })
    assert res.status_code == 200
    assert res.get_json()["type"] == "success"
