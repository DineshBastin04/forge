import io
import csv
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, Response
from flask_login import current_user
from sqlalchemy import text as _text

from auth import require_agent, admin_required, superadmin_required, log_audit_action, load_agent_queries, execute_dynamic_query
from db import get_engine
from db_config import get_config, load_configs
from log_buffers import log_device_reset, get_device_reset_logs
import notify

logger = logging.getLogger(__name__)
bp = Blueprint("device_reset", __name__)

DEFAULT_QUERIES = {
    "auto_scan": """
        SELECT device_id FROM t_log_message
        WHERE details LIKE '%Data Error%'
          AND device_id IS NOT NULL AND user_id IS NOT NULL
          AND logged_on_utc >= DATEADD(HOUR, -2, GETUTCDATE())
          AND logged_on_utc < GETUTCDATE()
    """,
    "find_employee": "SELECT id FROM t_employee WHERE device = :dev",
    "find_location": "SELECT wh_id, location_id FROM t_location WHERE c1 = :emp",
    "check_stored_item": "SELECT 1 FROM t_stored_item WHERE location_id = :l AND wh_id = :w",
    "check_hu_master": "SELECT 1 FROM t_hu_master WHERE location_id = :l AND wh_id = :w",
    "find_staging": """
        SELECT TOP 1 tl.location_id FROM t_location tl (NOLOCK)
        WHERE tl.wh_id = :wh AND (tl.status = 'E' OR tl.status = 'P') AND tl.type = 'S'
          AND (tl.description LIKE '%STAGE%' OR tl.description LIKE '%STAGING%')
          AND NOT EXISTS (SELECT 1 FROM t_stored_item si WHERE si.location_id = tl.location_id AND si.wh_id = tl.wh_id)
          AND NOT EXISTS (SELECT 1 FROM t_hu_master hm WHERE hm.location_id = tl.location_id AND hm.wh_id = tl.wh_id)
        ORDER BY tl.status ASC, ISNULL(tl.stored_qty, 0) ASC
    """,
    "update_employee": "UPDATE t_employee SET device = NULL WHERE id = :id AND wh_id = :wh AND device = :dev",
    "update_location": "UPDATE t_location SET c1 = NULL, status = 'E' WHERE location_id = :loc AND wh_id = :wh",
    "find_employee_by_id": "SELECT id FROM t_employee WHERE id = :id",
    "find_device_by_employee": "SELECT device FROM t_employee WHERE id = :id",
}


# ── Auto job ──────────────────────────────────────────────────────────────────

def _get_log_cfg(cfg: dict) -> dict:
    if "log_db" in cfg and cfg["log_db"]:
        return {
            "db_type": cfg.get("db_type", "mssql"),
            "db": cfg["log_db"]
        }
    return cfg


def auto_job():
    """Run device reset against all configured DB configs."""
    configs = load_configs()
    if not configs:
        return
    for config_id, cfg in configs.items():
        run_id = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        log_device_reset("INFO", f"Auto device reset started for {cfg.get('name', config_id)}.", run_id=run_id)
        try:
            results = _reset_all(config_id, cfg, run_id, executed_by="scheduler")
            notify.send_run_report(config_id, "Device Reset Agent", results, "scheduler")
        except Exception as e:
            logger.exception("Auto device reset failed for %s: %s", config_id, e)
            log_device_reset("ERROR", f"Auto job failed: {e}", run_id=run_id)
        finally:
            from log_buffers import flush_logs_to_db
            flush_logs_to_db(run_id, "device_reset")


def _reset_all(config_id, cfg, run_id, executed_by="scheduler"):
    log_cfg = _get_log_cfg(cfg)
    log_engine = get_engine(config_id + "_log" if "log_db" in cfg else config_id, log_cfg)
    queries = load_agent_queries("device_reset", DEFAULT_QUERIES)
    with log_engine.connect() as conn:
        rows = execute_dynamic_query(conn, queries["auto_scan"], {}).fetchall()

    if not rows:
        log_device_reset("INFO", "No stuck devices found.", run_id=run_id)
        return []

    device_ids = list({r[0] for r in rows})
    log_device_reset("INFO", f"Found {len(device_ids)} stuck device(s).", run_id=run_id)
    results = []
    ops_engine = get_engine(config_id, cfg)
    for device_id in device_ids:
        result = _reset_device_engine(ops_engine, device_id, run_id)
        results.append({"device_id": device_id, **result})
    return results


def _reset_device_engine(engine, device_id, run_id):
    log_device_reset("INFO", f"Processing device {device_id}.", device_id=device_id, run_id=run_id)
    queries = load_agent_queries("device_reset", DEFAULT_QUERIES)
    try:
        with engine.connect() as conn:
            emp_row = execute_dynamic_query(conn, queries["find_employee"], {"dev": device_id}).fetchone()
        if not emp_row:
            msg = "No employee record found — device not assigned."
            log_device_reset("WARNING", msg, device_id=device_id, run_id=run_id)
            return {"status": "WARNING", "message": msg}
        emp_id = emp_row[0]
        log_device_reset("INFO", f"Employee {emp_id} is assigned to device {device_id}.", device_id=device_id, run_id=run_id)

        with engine.connect() as conn:
            loc_row = execute_dynamic_query(conn, queries["find_location"], {"emp": emp_id}).fetchone()
        if not loc_row:
            msg = f"No fork location found for employee {emp_id}."
            log_device_reset("WARNING", msg, device_id=device_id, run_id=run_id)
            return {"status": "WARNING", "message": msg}
        wh_id, fork_loc = loc_row[0], loc_row[1]
        log_device_reset("INFO", f"Fork location {fork_loc} in warehouse {wh_id}.", device_id=device_id, run_id=run_id)

        with engine.connect() as conn:
            has_inv = bool(
                execute_dynamic_query(conn, queries["check_stored_item"], {"l": fork_loc, "w": wh_id}).fetchone()
                or execute_dynamic_query(conn, queries["check_hu_master"], {"l": fork_loc, "w": wh_id}).fetchone()
            )
        log_device_reset(
            "INFO",
            f"Inventory at {fork_loc}: {'present — relocation required' if has_inv else 'none — no relocation needed'}.",
            device_id=device_id, run_id=run_id,
        )

        temp_loc = None
        if has_inv:
            with engine.connect() as conn:
                stage = execute_dynamic_query(conn, queries["find_staging"], {"wh": wh_id}).fetchone()
            if not stage:
                msg = "No available staging location found to relocate inventory."
                log_device_reset("ERROR", msg, device_id=device_id, run_id=run_id)
                return {"status": "ERROR", "message": msg}
            temp_loc = stage[0]
            log_device_reset("INFO", f"Staging location {temp_loc} selected for relocation.", device_id=device_id, run_id=run_id)

        with engine.begin() as conn:
            if has_inv and temp_loc:
                for tbl in ("t_stored_item", "t_hu_master", "t_hu_detail"):
                    conn.execute(_text(f"UPDATE {tbl} SET location_id = :new WHERE location_id = :old AND wh_id = :wh"),
                                 {"new": temp_loc, "old": fork_loc, "wh": wh_id})
                log_device_reset("INFO", f"Inventory relocated from {fork_loc} to staging {temp_loc} (t_stored_item, t_hu_master, t_hu_detail).", device_id=device_id, run_id=run_id)
            execute_dynamic_query(conn, queries["update_employee"], {"id": emp_id, "wh": wh_id, "dev": device_id})
            log_device_reset("INFO", f"Device {device_id} cleared from employee {emp_id}.", device_id=device_id, run_id=run_id)
            execute_dynamic_query(conn, queries["update_location"], {"loc": fork_loc, "wh": wh_id})
            log_device_reset("INFO", f"Fork location {fork_loc} status reset to empty.", device_id=device_id, run_id=run_id)

        msg = f"Device {device_id} reset successfully."
        log_device_reset("INFO", msg, device_id=device_id, run_id=run_id)
        return {"status": "SUCCESS", "message": msg}

    except Exception as exc:
        msg = f"Reset failed: {exc}"
        logger.exception("Device reset failed for %s: %s", device_id, exc)
        log_device_reset("ERROR", msg, device_id=device_id, run_id=run_id)
        return {"status": "ERROR", "message": msg}


# ── Scheduler controls ────────────────────────────────────────────────────────

@bp.route("/api/v0/device_reset_agent/scheduler_status", methods=["GET"])
@require_agent("device_reset")
def scheduler_status():
    from scheduler import device_reset_job_info
    return jsonify(device_reset_job_info())


@bp.route("/api/v0/device_reset_agent/scheduler_toggle", methods=["POST"])
@admin_required
def scheduler_toggle():
    from scheduler import get_device_reset_scheduler
    s = get_device_reset_scheduler()
    if not s:
        return jsonify({"type": "error", "error": "Scheduler not initialized"}), 400
    job = s.get_job("identify_stuck_device")
    if not job:
        return jsonify({"type": "error", "error": "Job not found"}), 400
    if job.next_run_time is None:
        s.resume_job("identify_stuck_device"); action = "resumed"
    else:
        s.pause_job("identify_stuck_device"); action = "paused"
    log_audit_action(current_user.username, "SCHEDULER_TOGGLE", "device_reset", {"action": action})
    return jsonify({"type": "success", "action": action})


@bp.route("/api/v0/device_reset_agent/scheduler_interval", methods=["POST"])
@admin_required
def scheduler_interval():
    from scheduler import get_device_reset_scheduler
    s = get_device_reset_scheduler()
    if not s:
        return jsonify({"type": "error", "error": "Scheduler not initialized"}), 400
    data = request.get_json() or {}
    try:
        hours = float(data.get("hours", 0))
        if not (0.25 <= hours <= 168):
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"type": "error", "error": "hours must be between 0.25 and 168"}), 400
    s.reschedule_job("identify_stuck_device", trigger="interval", hours=hours)
    log_audit_action(current_user.username, "SCHEDULER_INTERVAL", "device_reset", {"hours": hours})
    return jsonify({"type": "success", "hours": hours})


# ── Manual reset ──────────────────────────────────────────────────────────────
from extensions import limiter

@bp.route("/api/v0/device_reset_agent/manual_reset", methods=["POST"])
@require_agent("device_reset")
@limiter.limit("5 per minute")
def manual_reset():
    data         = request.get_json() or {}
    db_config_id = data.get("db_config_id", "").strip()
    device_id    = str(data.get("device_id", "")).strip()
    input_type   = str(data.get("input_type", "device")).strip().lower()
    if not db_config_id or not device_id:
        return jsonify({"type": "error", "error": "db_config_id and device_id required"}), 400
    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404

    run_id = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    log_device_reset("INFO", f"Manual reset started for {input_type} {device_id}.", device_id=device_id, run_id=run_id)

    conn = None
    try:
        conn = get_engine(db_config_id, cfg).raw_connection()
        conn.autocommit = False
        cursor = conn.cursor()
        steps = []
        queries = load_agent_queries("device_reset", DEFAULT_QUERIES)

        emp_id = None
        resolved_device_id = None

        if input_type == "device":
            emp_row = execute_dynamic_query(cursor, queries["find_employee"], {"dev": device_id}).fetchone()
            if not emp_row:
                msg = f"No employee record for device {device_id}."
                log_device_reset("WARNING", msg, device_id=device_id, run_id=run_id)
                conn.rollback(); conn.close()
                return jsonify({"type": "warning", "message": msg})
            emp_id = emp_row[0]
            resolved_device_id = device_id
            steps.append("Resolved input as assigned Device ID. Employee record found.")
            log_device_reset("INFO", f"Employee {emp_id} found for device {device_id}.", device_id=device_id, run_id=run_id)
        else:
            # input_type is "employee"
            emp_by_id_query = queries.get("find_employee_by_id", "SELECT id FROM t_employee WHERE id = :id")
            emp_by_id_row = execute_dynamic_query(cursor, emp_by_id_query, {"id": device_id}).fetchone()
            if not emp_by_id_row:
                msg = f"No employee record found for Employee ID '{device_id}'."
                log_device_reset("WARNING", msg, device_id=device_id, run_id=run_id)
                conn.rollback(); conn.close()
                return jsonify({"type": "warning", "message": msg})
            emp_id = emp_by_id_row[0]
            steps.append(f"Resolved input as Employee ID. Employee record found for ID: {emp_id}.")
            log_device_reset("INFO", f"Employee {emp_id} found for Employee ID input '{device_id}'.", device_id=device_id, run_id=run_id)

            find_device_query = queries.get("find_device_by_employee", "SELECT device FROM t_employee WHERE id = :id")
            dev_row = execute_dynamic_query(cursor, find_device_query, {"id": emp_id}).fetchone()
            resolved_device_id = dev_row[0] if dev_row else None
            if resolved_device_id:
                steps.append(f"Found active assigned Device ID: {resolved_device_id}.")
                log_device_reset("INFO", f"Active device {resolved_device_id} found for employee {emp_id}.", device_id=device_id, run_id=run_id)
            else:
                steps.append("No active device assignment found for employee.")
                log_device_reset("INFO", f"No active device assignment for employee {emp_id}.", device_id=device_id, run_id=run_id)

        loc_row = execute_dynamic_query(cursor, queries["find_location"], {"emp": emp_id}).fetchone()
        if not loc_row:
            msg = f"No fork location found for employee {emp_id}."
            log_device_reset("WARNING", msg, device_id=device_id, run_id=run_id)
            conn.rollback(); conn.close()
            return jsonify({"type": "warning", "message": msg})
        wh_id, fork_loc = loc_row[0], loc_row[1]
        steps.append(f"Fork location: {fork_loc}, WH: {wh_id}.")
        log_device_reset("INFO", f"Fork location {fork_loc} in warehouse {wh_id}.", device_id=device_id, run_id=run_id)

        has_inv = bool(
            execute_dynamic_query(cursor, queries["check_stored_item"], {"l": fork_loc, "w": wh_id}).fetchone()
            or execute_dynamic_query(cursor, queries["check_hu_master"], {"l": fork_loc, "w": wh_id}).fetchone()
        )
        steps.append(f"Inventory at fork: {has_inv}.")
        log_device_reset("INFO", f"Inventory at fork {fork_loc}: {'present — relocation required' if has_inv else 'none'}.", device_id=device_id, run_id=run_id)

        temp_loc = None
        if has_inv:
            stage_row = execute_dynamic_query(cursor, queries["find_staging"], {"wh": wh_id}).fetchone()
            if not stage_row:
                msg = "No available staging location found."
                log_device_reset("ERROR", msg, device_id=device_id, run_id=run_id)
                conn.rollback(); conn.close()
                return jsonify({"type": "error", "message": msg})
            temp_loc = stage_row[0]
            steps.append(f"Staging location: {temp_loc}.")
            log_device_reset("INFO", f"Staging location {temp_loc} selected for inventory relocation.", device_id=device_id, run_id=run_id)

        if has_inv and temp_loc:
            for tbl in ("t_stored_item", "t_hu_master", "t_hu_detail"):
                cursor.execute(f"UPDATE {tbl} SET location_id = ? WHERE location_id = ? AND wh_id = ?",
                               (temp_loc, fork_loc, wh_id))
            steps.append(f"Inventory relocated to {temp_loc}.")
            log_device_reset("INFO", f"Inventory relocated from {fork_loc} to staging {temp_loc} (t_stored_item, t_hu_master, t_hu_detail).", device_id=device_id, run_id=run_id)

        if resolved_device_id:
            execute_dynamic_query(cursor, queries["update_employee"], {"id": emp_id, "wh": wh_id, "dev": resolved_device_id})
            steps.append("Device assignment cleared.")
            log_device_reset("INFO", f"Device {resolved_device_id} cleared from employee {emp_id}.", device_id=device_id, run_id=run_id)
        else:
            steps.append("No active device assignment to clear.")

        execute_dynamic_query(cursor, queries["update_location"], {"loc": fork_loc, "wh": wh_id})
        steps.append("Fork location reset.")
        log_device_reset("INFO", f"Fork location {fork_loc} status reset to empty.", device_id=device_id, run_id=run_id)

        conn.commit(); cursor.close()
        log_device_reset("INFO", f"Manual reset completed for {input_type} {device_id}.", device_id=device_id, run_id=run_id)
        notify.send_run_report(
            db_config_id, "Device Reset Agent",
            [{"status": "SUCCESS", "message": f"Device/Employee {device_id} reset"}],
            executed_by=current_user.display_name or current_user.username,
        )
        log_audit_action(current_user.username, "EXECUTE_MANUAL_RESET", device_id, {"db_config_id": db_config_id, "status": "SUCCESS", "steps": steps})
        return jsonify({"type": "success", "steps": steps})

    except Exception as exc:
        if conn:
            try: conn.rollback()
            except Exception: pass
        msg = f"Reset failed: {exc}"
        logger.exception("Manual device reset failed: %s", exc)
        log_device_reset("ERROR", msg, device_id=device_id, run_id=run_id)
        log_audit_action(current_user.username, "EXECUTE_MANUAL_RESET", device_id, {"db_config_id": db_config_id, "status": "FAILURE", "error": msg})
        return jsonify({"type": "error", "message": msg}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
        from log_buffers import flush_logs_to_db
        flush_logs_to_db(run_id, "device_reset")


# ── Auto scan & execute ───────────────────────────────────────────────────────

@bp.route("/api/v0/device_reset_agent/auto_scan", methods=["POST"])
@require_agent("device_reset")
def auto_scan():
    data = request.get_json() or {}
    db_config_id = data.get("db_config_id", "").strip()
    if not db_config_id:
        return jsonify({"type": "error", "error": "db_config_id is required"}), 400
    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404
    try:
        log_cfg = _get_log_cfg(cfg)
        log_engine = get_engine(db_config_id + "_log" if "log_db" in cfg else db_config_id, log_cfg)
        with log_engine.connect() as conn:
            rows = conn.execute(_text("""
                SELECT DISTINCT device_id FROM t_log_message
                WHERE details LIKE '%Data Error%'
                  AND device_id IS NOT NULL AND user_id IS NOT NULL
                  AND logged_on_utc >= DATEADD(HOUR, -2, GETUTCDATE())
                  AND logged_on_utc < GETUTCDATE()
            """)).fetchall()
        records = [{"device_id": str(r[0]).strip()} for r in rows]
        return jsonify({"type": "success", "records": records, "count": len(records)})
    except Exception as e:
        logger.exception("Auto scan failed for device reset: %s", e)
        return jsonify({"type": "error", "error": str(e)}), 500


@bp.route("/api/v0/device_reset_agent/execute", methods=["POST"])
@require_agent("device_reset")
@limiter.limit("5 per minute")
def execute():
    data = request.get_json() or {}
    db_config_id = data.get("db_config_id", "").strip()
    devices = data.get("devices", [])
    if not db_config_id:
        return jsonify({"type": "error", "error": "db_config_id is required"}), 400
    if not devices:
        return jsonify({"type": "error", "error": "No devices provided"}), 400
    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404

    run_id = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    log_device_reset("INFO", f"Manual auto-scan execute started. {len(devices)} device(s).", run_id=run_id)
    results = []
    try:
        engine = get_engine(db_config_id, cfg)
        for dev in devices:
            device_id = str(dev.get("device_id", "")).strip()
            if not device_id:
                results.append({"device_id": "", "status": "ERROR", "message": "Missing device_id"})
                continue
            res = _reset_device_engine(engine, device_id, run_id)
            results.append({"device_id": device_id, **res})

        notify.send_run_report(db_config_id, "Device Reset Agent", results, current_user.display_name or current_user.username)
        log_device_reset("INFO", f"Manual auto-scan execute completed. {len(results)} device(s) processed.", run_id=run_id)
        log_audit_action(current_user.username, "EXECUTE_AUTO_SCAN_RESET", db_config_id, {"devices_count": len(devices), "results": results})
        return jsonify({"type": "success", "results": results, "run_id": run_id})
    finally:
        from log_buffers import flush_logs_to_db
        flush_logs_to_db(run_id, "device_reset")


# ── Logs ──────────────────────────────────────────────────────────────────────

@bp.route("/api/v0/device_reset_logs", methods=["GET"])
@require_agent("device_reset")
def device_reset_logs():
    return jsonify({"logs": get_device_reset_logs()})


@bp.route("/api/v0/device_reset_logs/download", methods=["GET"])
@require_agent("device_reset")
def device_reset_logs_download():
    fmt = request.args.get("format", "csv").lower()
    entries = get_device_reset_logs()
    if fmt == "txt":
        lines = [
            "Device Reset Logs", "=" * 70,
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"Total entries: {len(entries)}", "",
        ]
        current_run = None
        for e in entries:
            if e["run_id"] != current_run:
                current_run = e["run_id"]
                lines += ["", f"Run: {current_run}", "-" * 50]
            dev = f"  Device: {e['device_id']}  |  " if e["device_id"] else "  "
            lines.append(f"  [{e['timestamp']}]  {e['level']:<9}{dev}{e['message']}")
        lines.append("")
        return Response("\n".join(lines), mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=device_reset_logs.txt"})
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Run ID", "Timestamp", "Level", "Device ID", "Message"])
    for e in entries:
        writer.writerow([e["run_id"], e["timestamp"], e["level"], e["device_id"], e["message"]])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=device_reset_logs.csv"})
