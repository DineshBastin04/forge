from collections import deque
from datetime import datetime
from threading import Lock

_device_reset_log  = deque(maxlen=500)
_device_reset_lock = Lock()
_device_reset_loaded = False

_unpick_log  = deque(maxlen=1000)
_unpick_lock = Lock()
_unpick_loaded = False


def _load_logs_from_db(log_type: str, target_deque):
    try:
        from auth import _get_conn
        conn = _get_conn()
        limit = target_deque.maxlen
        query = f"""
            SELECT TOP {limit} run_id, timestamp, level, device_id, wh_id, order_number, item_number, message
            FROM job_logs WHERE log_type = ?
            ORDER BY id DESC
        """
        rows = conn.execute(query, (log_type,)).fetchall()
        conn.close()

        for r in reversed(rows):
            if log_type == "device_reset":
                entry = {
                    "run_id": r[0],
                    "timestamp": r[1],
                    "level": r[2],
                    "device_id": r[3],
                    "message": r[7]
                }
            else:
                entry = {
                    "run_id": r[0],
                    "timestamp": r[1],
                    "level": r[2],
                    "wh_id": r[4],
                    "order_number": r[5],
                    "item_number": r[6],
                    "message": r[7]
                }
            target_deque.append(entry)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Could not load logs from DB: %s", e)


def log_device_reset(level: str, message: str, device_id: str = "", run_id: str = ""):
    entry = {
        "run_id":    run_id,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "level":     level,
        "device_id": device_id,
        "message":   message,
    }
    with _device_reset_lock:
        _device_reset_log.append(entry)


def log_unpick(level: str, message: str,
               order_number: str = "", item_number: str = "",
               wh_id: str = "", run_id: str = ""):
    entry = {
        "run_id":       run_id,
        "timestamp":    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "level":        level,
        "wh_id":        wh_id,
        "order_number": order_number,
        "item_number":  item_number,
        "message":      message,
    }
    with _unpick_lock:
        _unpick_log.append(entry)


def get_device_reset_logs():
    global _device_reset_loaded
    with _device_reset_lock:
        if not _device_reset_loaded:
            _load_logs_from_db("device_reset", _device_reset_log)
            _device_reset_loaded = True
        return list(_device_reset_log)


def get_unpick_logs():
    global _unpick_loaded
    with _unpick_lock:
        if not _unpick_loaded:
            _load_logs_from_db("unpick", _unpick_log)
            _unpick_loaded = True
        return list(_unpick_log)


def flush_logs_to_db(run_id: str, log_type: str):
    if not run_id:
        return
    if log_type == "device_reset":
        with _device_reset_lock:
            entries = [e for e in _device_reset_log if e["run_id"] == run_id]
    elif log_type == "unpick":
        with _unpick_lock:
            entries = [e for e in _unpick_log if e["run_id"] == run_id]
    else:
        return

    if not entries:
        return

    try:
        from auth import _get_conn
        conn = _get_conn()
        for e in entries:
            conn.execute(
                "INSERT INTO job_logs (log_type, run_id, timestamp, level, device_id, wh_id, order_number, item_number, message)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    log_type,
                    e["run_id"],
                    e["timestamp"],
                    e["level"],
                    e.get("device_id"),
                    e.get("wh_id"),
                    e.get("order_number"),
                    e.get("item_number"),
                    e["message"]
                )
            )
        conn.commit()
        conn.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Failed to flush logs to DB: %s", e)
