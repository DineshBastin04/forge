import os
import threading
import logging
from urllib.parse import quote_plus

import pyodbc
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

_engine_cache: dict = {}
_engine_lock = threading.Lock()

_DEFAULT_DRIVERS = {
    "mssql":  "ODBC Driver 17 for SQL Server",
    "oracle": "Oracle in OraDB21Home1",
}

_DEFAULT_PORTS = {
    "mssql":  "1433",
    "oracle": "1521",
}


def _build_conn_str(cfg: dict) -> str:
    db      = cfg.get("db", {})
    db_type = cfg.get("db_type", "mssql").lower()
    server   = db.get("server", "")
    port     = db.get("port", "") or _DEFAULT_PORTS.get(db_type, "1433")
    database = db.get("database", "")
    username = db.get("username", "")
    password = db.get("password", "")
    driver   = db.get("driver", "") or _DEFAULT_DRIVERS.get(db_type, _DEFAULT_DRIVERS["mssql"])

    if db_type == "oracle":
        # Oracle ODBC: DBQ = host:port/service_name (TNS-style)
        return (
            f"DRIVER={{{driver}}};"
            f"DBQ={server}:{port}/{database};"
            f"UID={username};PWD={password}"
        )
    # Default: MSSQL
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server},{port};"
        f"DATABASE={database};"
        f"UID={username};PWD={password}"
    )


def _build_engine(cfg: dict):
    db_type  = cfg.get("db_type", "mssql").lower()
    conn_str = _build_conn_str(cfg)

    if db_type == "oracle":
        url = f"oracle+pyodbc://?odbc_connect={quote_plus(conn_str)}"
    else:
        url = f"mssql+pyodbc://?odbc_connect={quote_plus(conn_str)}"

    pool_size    = int(os.getenv("DB_POOL_SIZE", "5"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=30,
        pool_pre_ping=True,
    )
    db = cfg.get("db", {})
    logger.info("Engine ready — type=%s server=%s db=%s", db_type, db.get("server"), db.get("database"))
    return engine


def get_engine(db_config_id: str, cfg: dict):
    with _engine_lock:
        if db_config_id not in _engine_cache:
            _engine_cache[db_config_id] = _build_engine(cfg)
        return _engine_cache[db_config_id]


def invalidate_engine(db_config_id: str):
    with _engine_lock:
        engine = _engine_cache.pop(db_config_id, None)
    if engine:
        try:
            engine.dispose()
        except Exception:
            pass


def get_pool_status() -> dict:
    with _engine_lock:
        status = {}
        for config_id, engine in _engine_cache.items():
            pool = engine.pool
            status[config_id] = {
                "size": pool.size(),
                "checkedin": pool.checkedin(),
                "checkedout": pool.checkedout(),
                "overflow": pool.overflow() if hasattr(pool, "overflow") else 0,
            }
        return status



def pyodbc_connect(cfg: dict):
    conn_str = _build_conn_str(cfg)
    conn = pyodbc.connect(conn_str, timeout=10)
    conn.autocommit = False
    return conn
