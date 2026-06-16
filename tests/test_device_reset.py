import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import text
from agents.device_reset import _reset_device_engine

def test_device_reset_success_no_inventory():
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.begin.return_value.__enter__.return_value = conn

    # Mock fetchone calls:
    # 1. employee id check -> (123,)
    # 2. fork location check -> ("WH1", "LOC-FORK")
    # 3. t_stored_item inventory check -> None
    # 4. t_hu_master inventory check -> None
    mock_res = MagicMock()
    mock_res.fetchone.side_effect = [
        (123,),
        ("WH1", "LOC-FORK"),
        None,
        None
    ]
    conn.execute.return_value = mock_res

    res = _reset_device_engine(engine, "DEV001", "RUN001")

    assert res["status"] == "SUCCESS"
    assert "reset complete" in res["message"]


def test_device_reset_employee_not_found():
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    # Mock employee id check -> None (no record)
    mock_res = MagicMock()
    mock_res.fetchone.return_value = None
    conn.execute.return_value = mock_res

    res = _reset_device_engine(engine, "DEV001", "RUN001")

    assert res["status"] == "WARNING"
    assert "No employee record found" in res["message"]


def test_device_reset_location_not_found():
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    # Mock employee id check -> (123,)
    # Mock fork location check -> None (no location)
    mock_res = MagicMock()
    mock_res.fetchone.side_effect = [
        (123,),
        None
    ]
    conn.execute.return_value = mock_res

    res = _reset_device_engine(engine, "DEV001", "RUN001")

    assert res["status"] == "WARNING"
    assert "No fork location found" in res["message"]


def test_device_reset_with_inventory_relocation():
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn
    engine.begin.return_value.__enter__.return_value = conn

    # Mock fetchone calls:
    # 1. employee id check -> (123,)
    # 2. fork location check -> ("WH1", "LOC-FORK")
    # 3. t_stored_item inventory check -> (1,) (inventory exists!)
    # 4. staging location lookup -> ("LOC-STAGE-01",)
    mock_res = MagicMock()
    mock_res.fetchone.side_effect = [
        (123,),
        ("WH1", "LOC-FORK"),
        (1,),
        ("LOC-STAGE-01",)
    ]
    conn.execute.return_value = mock_res

    res = _reset_device_engine(engine, "DEV001", "RUN001")

    assert res["status"] == "SUCCESS"
    assert "reset complete" in res["message"]
    # Check that update queries were executed
    assert conn.execute.called


from flask import Flask, jsonify
from flask_login import LoginManager
from agents.device_reset import bp as device_reset_bp

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

    app.register_blueprint(device_reset_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@patch("agents.device_reset.get_config")
@patch("agents.device_reset.get_engine")
def test_auto_scan_endpoint_success(mock_get_engine, mock_get_config, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "1"
    user.has_agent_perm.return_value = True

    app.config["test_users"] = {"1": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "1"

    mock_get_config.return_value = {"name": "TestDB", "db_type": "mssql"}
    
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_get_engine.return_value = mock_engine
    mock_engine.connect.return_value.__enter__.return_value = mock_conn

    # Mock DB query results for stuck devices: returns two devices
    mock_res = MagicMock()
    mock_res.fetchall.return_value = [("DEV001",), ("DEV002",)]
    mock_conn.execute.return_value = mock_res

    payload = {"db_config_id": "TestDB"}
    res = client.post("/api/v0/device_reset_agent/auto_scan", json=payload)
    
    assert res.status_code == 200
    data = res.get_json()
    assert data["type"] == "success"
    device_ids = [r["device_id"] for r in data["records"]]
    assert "DEV001" in device_ids
    assert "DEV002" in device_ids
    assert data["count"] == 2


@patch("agents.device_reset._reset_device_engine")
@patch("agents.device_reset.get_config")
@patch("agents.device_reset.get_engine")
@patch("agents.device_reset.notify.send_run_report")
def test_execute_scan_endpoint_success(mock_notify, mock_get_engine, mock_get_config, mock_reset_device_engine, app, client):
    user = MagicMock()
    user.is_authenticated = True
    user.is_active = True
    user.get_id.return_value = "2"
    user.has_agent_perm.return_value = True
    user.username = "testadmin"

    app.config["test_users"] = {"2": user}

    with client.session_transaction() as sess:
        sess["_user_id"] = "2"

    mock_get_config.return_value = {"name": "TestDB", "db_type": "mssql"}
    mock_get_engine.return_value = MagicMock()

    mock_reset_device_engine.side_effect = [
        {"status": "SUCCESS", "message": "Device DEV001 reset complete."},
        {"status": "WARNING", "message": "No employee record found."}
    ]

    payload = {
        "db_config_id": "TestDB",
        "devices": [{"device_id": "DEV001"}, {"device_id": "DEV002"}]
    }

    res = client.post("/api/v0/device_reset_agent/execute", json=payload)
    
    assert res.status_code == 200
    data = res.get_json()
    assert data["type"] == "success"
    assert len(data["results"]) == 2
    assert data["results"][0] == {"device_id": "DEV001", "status": "SUCCESS", "message": "Device DEV001 reset complete."}
    assert data["results"][1] == {"device_id": "DEV002", "status": "WARNING", "message": "No employee record found."}
    mock_notify.assert_called_once()


from agents.device_reset import _get_log_cfg

def test_get_log_cfg():
    # Primary only config
    cfg_primary = {
        "db_type": "mssql",
        "db": {
            "server": "primary_host",
            "database": "primary_db"
        }
    }
    assert _get_log_cfg(cfg_primary) == cfg_primary

    # Dual DB config
    cfg_dual = {
        "db_type": "mssql",
        "db": {
            "server": "primary_host",
            "database": "primary_db"
        },
        "log_db": {
            "server": "log_host",
            "database": "log_db"
        }
    }
    log_cfg = _get_log_cfg(cfg_dual)
    assert log_cfg["db_type"] == "mssql"
    assert log_cfg["db"] == cfg_dual["log_db"]


@patch("agents.device_reset._reset_device_engine")
@patch("agents.device_reset.get_config")
@patch("agents.device_reset.get_engine")
def test_reset_all_with_dual_db(mock_get_engine, mock_get_config, mock_reset_device_engine):
    from agents.device_reset import _reset_all

    cfg_dual = {
        "db_type": "mssql",
        "db": {
            "server": "primary_host",
            "database": "primary_db"
        },
        "log_db": {
            "server": "log_host",
            "database": "log_db"
        }
    }
    mock_get_config.return_value = cfg_dual

    # We need two separate engines: one for log_db, one for operational db
    mock_log_engine = MagicMock()
    mock_ops_engine = MagicMock()
    mock_get_engine.side_effect = lambda cid, c: mock_log_engine if cid.endswith("_log") else mock_ops_engine

    mock_log_conn = MagicMock()
    mock_log_engine.connect.return_value.__enter__.return_value = mock_log_conn
    # Mock finding one stuck device
    mock_res = MagicMock()
    mock_res.fetchall.return_value = [("DEV001",)]
    mock_log_conn.execute.return_value = mock_res

    # Mock the reset engine return values
    mock_reset_device_engine.return_value = {"status": "SUCCESS", "message": "Reset complete."}

    results = _reset_all("TestDB", cfg_dual, "RUN001")

    # Assertions
    assert len(results) == 1
    assert results[0]["device_id"] == "DEV001"
    
    # Verify log_engine queried t_log_message
    assert mock_log_conn.execute.called
    called_sql = mock_log_conn.execute.call_args[0][0]
    assert "t_log_message" in str(called_sql)

    # Verify reset engine was executed on the operational database engine
    mock_reset_device_engine.assert_called_once_with(mock_ops_engine, "DEV001", "RUN001")
