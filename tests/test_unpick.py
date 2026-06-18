import pytest
from unittest.mock import MagicMock
from agents.unpick import _do_unpick_pyodbc, _do_manual_unpick_pyodbc

def test_unpick_success():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # Mock sequence of fetchone calls:
    # 1. picked_quantity -> (10.0,)
    # 2. sys.columns check -> (1,) (indicates pick_location column exists)
    # 3. pick_location -> ("LOC-101",)
    # 4. item_hu_indicator -> ("I",)
    # 5. t_stored_item exists check -> (1,)
    # 6. hu_id check -> None
    cursor.fetchone.side_effect = [
        (10.0,),
        (1,),
        ("LOC-101",),
        ("I",),
        (1,),
        None
    ]

    res = _do_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "SUCCESS"
    assert "Unpick completed" in res["message"]
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_unpick_zero_quantity():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # Mock picked_quantity -> (0.0,)
    cursor.fetchone.side_effect = [(0.0,)]

    res = _do_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "WARNING"
    assert "picked_quantity is 0 or NULL" in res["message"]
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_unpick_qty_exceeds_picked():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # Mock picked_quantity -> (5.0,)
    cursor.fetchone.side_effect = [(5.0,)]

    # We try to unpick 10.0 which exceeds 5.0
    res = _do_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1", qty=10.0)

    assert res["status"] == "WARNING"
    assert "exceeds picked_quantity" in res["message"]
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_unpick_missing_location():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # 1. picked_quantity -> (10.0,)
    # 2. sys.columns check -> (1,)
    # 3. pick_location -> None
    # 4. t_tran_log check -> None (no location resolved)
    cursor.fetchone.side_effect = [
        (10.0,),
        (1,),
        (None,),
        None
    ]

    res = _do_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "WARNING"
    assert "Could not resolve pick_location" in res["message"]
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_unpick_db_error_triggers_rollback():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # Force an exception on execute
    cursor.execute.side_effect = Exception("DB Connection Lost")

    res = _do_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "ERROR"
    assert "All changes rolled back" in res["message"]
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_manual_unpick_success_item_controlled():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # Mock sequence of fetchone calls:
    # 1. sys.columns check -> (1,)
    # 2. pick_location -> ("LOC-101",)
    # 3. item_hu_indicator -> ("I",)
    # 4. hu_id -> ("HU-001",)
    # 5. exists_stored -> (1,)
    # 6. check empty LP -> None
    cursor.fetchone.side_effect = [
        (1,),
        ("LOC-101",),
        ("I",),
        ("HU-001",),
        (1,),
        None
    ]

    res = _do_manual_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "SUCCESS"
    assert "Manual unpick completed successfully" in res["message"]
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_manual_unpick_success_lp_single_item():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # Mock sequence of fetchone calls:
    # 1. sys.columns check -> (1,)
    # 2. pick_location -> ("LOC-101",)
    # 3. item_hu_indicator -> ("H",)
    # 4. hu_id -> ("HU-002",)
    # 5. item_count -> (1,)
    # 6. check empty LP -> None
    # 7. exists_stored check -> None
    cursor.fetchone.side_effect = [
        (1,),
        ("LOC-101",),
        ("H",),
        ("HU-002",),
        (1,),
        None,
        None
    ]

    res = _do_manual_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "SUCCESS"
    assert "Manual unpick completed successfully" in res["message"]
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_manual_unpick_success_lp_multi_item():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # Mock sequence of fetchone calls:
    # 1. sys.columns check -> (1,)
    # 2. pick_location -> ("LOC-101",)
    # 3. item_hu_indicator -> ("H",)
    # 4. hu_id -> ("HU-003",)
    # 5. item_count -> (3,)
    # 6. exists_stored check -> (1,)
    cursor.fetchone.side_effect = [
        (1,),
        ("LOC-101",),
        ("H",),
        ("HU-003",),
        (3,),
        (1,)
    ]

    res = _do_manual_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "SUCCESS"
    assert "Manual unpick completed successfully" in res["message"]
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_manual_unpick_missing_location():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    # 1. sys.columns check -> (1,)
    # 2. pick_location -> None
    # 3. tran log search -> None
    cursor.fetchone.side_effect = [
        (1,),
        (None,),
        None
    ]

    res = _do_manual_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "WARNING"
    assert "Could not resolve pick_location" in res["message"]
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_manual_unpick_db_error_triggers_rollback():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    conn.cursor.return_value = cursor

    cursor.execute.side_effect = Exception("Connection lost")

    res = _do_manual_unpick_pyodbc(conn, "WH1", "ORD1", "ITEM1", "RUN1")

    assert res["status"] == "ERROR"
    assert "All changes rolled back" in res["message"]
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


from flask import Flask, jsonify
from flask_login import LoginManager
from agents.unpick import bp as unpick_bp

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

    app.register_blueprint(unpick_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


from unittest.mock import patch

@patch("agents.unpick._do_manual_unpick_pyodbc")
@patch("agents.unpick.get_config")
@patch("agents.unpick.get_engine")
@patch("agents.unpick.notify.send_run_report")
def test_manual_unpick_endpoint_success(mock_notify, mock_get_engine, mock_get_config, mock_do_manual_unpick, app, client):
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
    mock_get_engine.return_value = mock_engine

    mock_do_manual_unpick.return_value = {"status": "SUCCESS", "message": "Manual unpick completed successfully."}

    payload = {
        "db_config_id": "TestDB",
        "wh_id": "WH1",
        "order_number": "ORD1",
        "item_number": "ITEM1"
    }
    res = client.post("/api/v0/unpick_agent/manual_unpick", json=payload)

    assert res.status_code == 200
    data = res.get_json()
    assert data["type"] == "success"
    assert "Manual unpick completed successfully" in data["message"]
    mock_notify.assert_called_once()

