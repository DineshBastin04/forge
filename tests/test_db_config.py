import pytest
from unittest.mock import MagicMock, patch
from flask import Flask, jsonify
from flask_login import LoginManager
from db_config import bp as db_config_bp


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

    app.register_blueprint(db_config_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_test_connection_requires_admin_anonymous(client):
    res = client.post("/api/v0/admin/db_configs/test_connection", json={})
    assert res.status_code == 401
    assert res.get_json()["error"] == "Not authenticated"


def test_test_connection_requires_admin_non_admin(app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.is_admin.return_value = False

    app.config["test_users"] = {"1": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "1"

    res = client.post("/api/v0/admin/db_configs/test_connection", json={})
    assert res.status_code == 403
    assert res.get_json()["error"] == "Admin access required"


@patch("db.pyodbc_connect")
def test_test_connection_success(mock_connect, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "2"
    user.is_admin.return_value = True

    app.config["test_users"] = {"2": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "2"

    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn

    payload = {
        "name": "TestDB",
        "db_type": "mssql",
        "db": {
            "server": "localhost",
            "port": "1433",
            "database": "test_db",
            "username": "sa",
            "password": "my_password",
            "driver": "ODBC Driver 17 for SQL Server"
        }
    }

    res = client.post("/api/v0/admin/db_configs/test_connection", json=payload)
    assert res.status_code == 200
    assert res.get_json() == {"type": "success", "message": "Connection successful"}

    # Assert that pyodbc_connect was called with the expected config dict
    mock_connect.assert_called_once()
    called_cfg = mock_connect.call_args[0][0]
    assert called_cfg["db_type"] == "mssql"
    assert called_cfg["db"]["password"] == "my_password"
    assert called_cfg["db"]["database"] == "test_db"
    assert mock_conn.close.called


@patch("db.pyodbc_connect")
def test_test_connection_failure(mock_connect, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "3"
    user.is_admin.return_value = True

    app.config["test_users"] = {"3": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "3"

    mock_connect.side_effect = Exception("OperationalError: Access denied")

    payload = {
        "name": "TestDB",
        "db_type": "mssql",
        "db": {
            "server": "localhost",
            "port": "1433",
            "database": "test_db",
            "username": "sa",
            "password": "wrong_password"
        }
    }

    res = client.post("/api/v0/admin/db_configs/test_connection", json=payload)
    assert res.status_code == 500
    assert res.get_json()["type"] == "error"
    assert "Access denied" in res.get_json()["error"]


@patch("db_config.get_config")
@patch("db.pyodbc_connect")
def test_test_connection_masked_password_lookup(mock_connect, mock_get_config, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "4"
    user.is_admin.return_value = True

    app.config["test_users"] = {"4": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "4"

    # Mock the existing config in db_configs.json (already decrypted password)
    mock_get_config.return_value = {
        "db_type": "mssql",
        "db": {
            "server": "localhost",
            "port": "1433",
            "database": "test_db",
            "username": "sa",
            "password": "actual_saved_password"
        }
    }

    payload = {
        "name": "TestDB",
        "db_type": "mssql",
        "db": {
            "server": "localhost",
            "port": "1433",
            "database": "test_db",
            "username": "sa",
            "password": "***" # User keeps password unchanged
        }
    }

    res = client.post("/api/v0/admin/db_configs/test_connection", json=payload)
    assert res.status_code == 200
    assert res.get_json()["type"] == "success"

    mock_get_config.assert_called_once_with("TestDB")
    mock_connect.assert_called_once()
    called_cfg = mock_connect.call_args[0][0]
    assert called_cfg["db"]["password"] == "actual_saved_password"


@patch("db.pyodbc_connect")
def test_test_connection_log_db_success(mock_connect, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "5"
    user.is_admin.return_value = True

    app.config["test_users"] = {"5": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "5"

    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn

    payload = {
        "name": "TestDB",
        "db_type": "mssql",
        "target": "log",
        "log_db": {
            "server": "loghost",
            "port": "1433",
            "database": "log_db",
            "username": "logsa",
            "password": "log_password"
        }
    }

    res = client.post("/api/v0/admin/db_configs/test_connection", json=payload)
    assert res.status_code == 200
    assert res.get_json() == {"type": "success", "message": "Connection successful"}

    mock_connect.assert_called_once()
    called_cfg = mock_connect.call_args[0][0]
    assert called_cfg["db_type"] == "mssql"
    assert called_cfg["db"]["password"] == "log_password"
    assert called_cfg["db"]["database"] == "log_db"
    assert called_cfg["db"]["server"] == "loghost"


@patch("db_config.get_config")
@patch("db.pyodbc_connect")
def test_test_connection_log_db_masked_password_lookup(mock_connect, mock_get_config, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "6"
    user.is_admin.return_value = True

    app.config["test_users"] = {"6": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "6"

    mock_get_config.return_value = {
        "db_type": "mssql",
        "db": {
            "server": "localhost",
            "password": "primary_password"
        },
        "log_db": {
            "server": "loghost",
            "password": "actual_log_password"
        }
    }

    payload = {
        "name": "TestDB",
        "db_type": "mssql",
        "target": "log",
        "log_db": {
            "server": "loghost",
            "password": "***"
        }
    }

    res = client.post("/api/v0/admin/db_configs/test_connection", json=payload)
    assert res.status_code == 200
    assert res.get_json()["type"] == "success"

    mock_get_config.assert_called_once_with("TestDB")
    mock_connect.assert_called_once()
    called_cfg = mock_connect.call_args[0][0]
    assert called_cfg["db"]["password"] == "actual_log_password"
