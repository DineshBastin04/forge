import io
import csv
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, Response
from flask_login import current_user
from sqlalchemy import text as _text

from auth import require_agent, admin_required, superadmin_required, log_audit_action, load_agent_queries, execute_dynamic_query
from db import get_engine, pyodbc_connect
from db_config import get_config, load_configs
from extensions import limiter
from log_buffers import log_unpick, get_unpick_logs
import notify

logger = logging.getLogger(__name__)
bp = Blueprint("unpick", __name__)

_AUTO_UNPICK_SQL = """
    SELECT DISTINCT TL.control_number AS order_number, TL.wh_id, TL.item_number
    FROM t_tran_log TL WITH(NOLOCK)
    LEFT JOIN t_pick_detail PD ON PD.order_number = TL.control_number AND PD.wh_id = TL.wh_id
        AND PD.item_number = TL.item_number AND PD.line_number = TL.line_number
    LEFT JOIN t_stored_item SI ON SI.wh_id = TL.wh_id AND SI.item_number = TL.item_number
        AND SI.type = TL.control_number
    LEFT JOIN t_hu_master HM ON HM.wh_id = TL.wh_id AND HM.hu_id = TL.hu_id
    LEFT JOIN t_hu_detail HD ON HD.wh_id = TL.wh_id AND HD.hu_id = TL.hu_id
        AND HD.item_number = TL.item_number
    LEFT JOIN t_work_q WQ ON WQ.wh_id = TL.wh_id AND WQ.pick_ref_number = TL.control_number
        AND WQ.item_number = TL.item_number
    WHERE TL.tran_type = '391' AND TL.description = 'Unload/Unpick (pick)'
    AND NOT EXISTS (
        SELECT 1 FROM t_tran_log TL2
        WHERE TL2.control_number = TL.control_number AND TL2.wh_id = TL.wh_id
          AND TL2.item_number = TL.item_number
          AND TL2.tran_type = '301' AND TL2.description = 'Picking (pick)'
          AND (CAST(TL2.start_tran_date AS DATETIME) + CAST(TL2.start_tran_time AS DATETIME)
               > CAST(TL.start_tran_date AS DATETIME) + CAST(TL.start_tran_time AS DATETIME))
    )
    AND (ISNULL(PD.picked_quantity, 0) <> 0 OR ISNULL(PD.staged_quantity, 0) <> 0
        OR PD.status <> 'RELEASED' OR SI.type <> 'STORAGE'
        OR SI.location_id <> (SELECT TOP 1 TL_PICK.location_id FROM t_tran_log TL_PICK
            WHERE TL_PICK.control_number = TL.control_number AND TL_PICK.wh_id = TL.wh_id
              AND TL_PICK.item_number = TL.item_number AND TL_PICK.tran_type = '301'
            ORDER BY TL_PICK.start_tran_date DESC, TL_PICK.start_tran_time DESC)
        OR HM.control_number IS NOT NULL OR HM.type <> 'IV'
        OR HD.storage_type IS NOT NULL OR WQ.work_status <> 'U')
"""

DEFAULT_QUERIES = {
    "auto_scan": _AUTO_UNPICK_SQL,
    "find_picked_qty": "SELECT picked_quantity FROM t_pick_detail WHERE wh_id = :w AND order_number = :o AND item_number = :i",
    "check_columns": "SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('t_pick_detail') AND name = 'pick_location'",
    "find_pick_loc": "SELECT pick_location FROM t_pick_detail WHERE wh_id = :w AND order_number = :o AND item_number = :i",
    "find_tran_loc_301": "SELECT TOP 1 location_id FROM t_tran_log WHERE wh_id = :w AND tran_type = '301' AND item_number = :i AND control_number = :o ORDER BY start_tran_date DESC, start_tran_time DESC",
    "find_tran_source_loc_301": "SELECT TOP 1 source_location_id FROM t_tran_log WHERE wh_id = :w AND tran_type = '301' AND item_number = :i AND control_number = :o ORDER BY start_tran_date DESC, start_tran_time DESC",
    "update_pick_detail": """
        UPDATE t_pick_detail
        SET staged_quantity = CASE WHEN staged_quantity >= :q THEN staged_quantity - :q ELSE 0 END,
            picked_quantity = picked_quantity - :q,
            status = CASE WHEN picked_quantity - :q > 0 THEN 'PICKED' ELSE 'RELEASED' END
        WHERE wh_id = :w AND order_number = :o AND item_number = :i
    """,
    "check_item_indicator": "SELECT item_hu_indicator FROM t_location WHERE wh_id = :w AND location_id = :l",
    "check_stored_item": "SELECT 1 FROM t_stored_item WHERE wh_id = :w AND item_number = :i AND type = 'STORAGE' AND location_id = :l",
    "update_stored_qty_add": "UPDATE t_stored_item SET actual_qty = actual_qty + :q WHERE wh_id = :w AND item_number = :i AND type = 'STORAGE' AND location_id = :l",
    "update_stored_qty_move": "UPDATE t_stored_item SET type = 'STORAGE', location_id = :l, actual_qty = :q WHERE wh_id = :w AND item_number = :i AND type = :o",
    "update_stored_qty_sub": "UPDATE t_stored_item SET actual_qty = actual_qty - :q WHERE wh_id = :w AND item_number = :i AND type = :o",
    "delete_empty_stored_item": "DELETE FROM t_stored_item WHERE wh_id = :w AND item_number = :i AND type = :o AND actual_qty <= 0",
    "find_hu_id": "SELECT TOP 1 hu_id FROM t_hu_detail WHERE wh_id = :w AND item_number = :i AND storage_type = :o",
    "update_hu_qty_sub": "UPDATE t_hu_detail SET actual_qty = actual_qty - :q WHERE wh_id = :w AND hu_id = :h AND item_number = :i AND storage_type = :o",
    "delete_empty_hu_detail": "DELETE FROM t_hu_detail WHERE wh_id = :w AND hu_id = :h AND item_number = :i AND storage_type = :o AND actual_qty <= 0",
    "check_hu_detail": "SELECT 1 FROM t_hu_detail WHERE wh_id = :w AND hu_id = :h",
    "delete_empty_hu_master": "DELETE FROM t_hu_master WHERE wh_id = :w AND hu_id = :h",
    "update_work_q": """
        UPDATE t_work_q SET work_status = CASE
            WHEN EXISTS (SELECT 1 FROM t_pick_detail pd WHERE pd.work_q_id = t_work_q.work_q_id
                         AND pd.picked_quantity >= pd.planned_quantity) THEN 'C' ELSE 'U' END
        WHERE wh_id = :w AND pick_ref_number = :o
          AND work_q_id IN (SELECT work_q_id FROM t_pick_detail WHERE order_number = :o AND wh_id = :w AND item_number = :i)
    """,
    "manual_unpick_update_pick": """
        UPDATE t_pick_detail
        SET staged_quantity = 0,
            picked_quantity = 0,
            status = 'RELEASED'
        WHERE order_number = :o
          AND wh_id = :w
          AND item_number = :i
    """,
    "manual_update_stored_qty_add": """
        UPDATE S
           SET S.actual_qty = S.actual_qty + O.actual_qty
        FROM t_stored_item S
        JOIN t_stored_item O
          ON O.wh_id = S.wh_id
         AND O.item_number = S.item_number
        WHERE S.wh_id = :w
          AND S.item_number = :i
          AND S.type = 'STORAGE'
          AND S.location_id = :l
          AND O.type = :o
    """,
    "manual_delete_stored_item": "DELETE FROM t_stored_item WHERE wh_id = :w AND item_number = :i AND type = :o",
    "manual_update_stored_item_move": """
        UPDATE t_stored_item
        SET type = 'STORAGE',
            location_id = :l
        WHERE wh_id = :w AND item_number = :i AND type = :o
    """,
    "manual_delete_hu_detail": "DELETE FROM t_hu_detail WHERE wh_id = :w AND hu_id = :h AND item_number = :i AND storage_type = :o",
    "manual_get_distinct_item_count": "SELECT COUNT(DISTINCT item_number) FROM t_hu_detail WHERE wh_id = :w AND hu_id = :h",
    "manual_insert_hu_master": "INSERT INTO t_hu_master (hu_id, type, control_number, location_id, status, wh_id) VALUES (:h, 'LP', NULL, :l, 'A', :w)",
    "manual_update_hu_detail_multi": """
        UPDATE t_hu_detail
        SET hu_id = :nh,
            location_id = :l,
            storage_type = NULL
        WHERE wh_id = :w
          AND hu_id = :oh
          AND item_number = :i
    """,
    "manual_update_work_q": """
        UPDATE t_work_q
        SET work_status = 'U'
        WHERE pick_ref_number = :o
          AND wh_id = :w
          AND work_q_id IN
        (
            SELECT work_q_id
            FROM t_pick_detail
            WHERE order_number = :o
              AND wh_id = :w
              AND item_number = :i
        )
    """,
}


# ── Auto job ──────────────────────────────────────────────────────────────────

def auto_job():
    """Run auto-unpick against all configured DB configs."""
    configs = load_configs()
    if not configs:
        return
    for config_id, cfg in configs.items():
        run_id = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        log_unpick("INFO", f"Auto unpick started for {cfg.get('name', config_id)}.", run_id=run_id)
        try:
            results = _run_auto(config_id, cfg, run_id)
            notify.send_run_report(config_id, "Unpick Agent", results, "scheduler")
        except Exception as e:
            logger.exception("Auto unpick failed for %s: %s", config_id, e)
            log_unpick("ERROR", f"Auto job failed: {e}", run_id=run_id)
        finally:
            from log_buffers import flush_logs_to_db
            flush_logs_to_db(run_id, "unpick")


def _run_auto(config_id, cfg, run_id):
    engine = get_engine(config_id, cfg)
    queries = load_agent_queries("unpick", DEFAULT_QUERIES)
    with engine.connect() as conn:
        rows = execute_dynamic_query(conn, queries["auto_scan"], {}).fetchall()

    if not rows:
        log_unpick("INFO", "No dirty unpick records found.", run_id=run_id)
        return []

    records = [{"order_number": str(r[0]).strip(), "wh_id": str(r[1]).strip(), "item_number": str(r[2]).strip()} for r in rows]
    log_unpick("INFO", f"Found {len(records)} record(s) to process.", run_id=run_id)
    results = []
    for rec in records:
        result = _do_unpick_engine(engine, rec["wh_id"], rec["order_number"], rec["item_number"], run_id)
        results.append({**rec, **result})
    return results


# ── Shared unpick logic (SQLAlchemy engine) ───────────────────────────────────

def _resolve_pick_loc_conn(conn, wh_id, order_number, item_number, queries):
    row = execute_dynamic_query(conn, queries["check_columns"], {}).fetchone()
    if row:
        row = execute_dynamic_query(conn, queries["find_pick_loc"], {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
        loc = row[0] if row else None
        if not loc:
            row2 = execute_dynamic_query(conn, queries["find_tran_loc_301"], {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
            loc = row2[0] if row2 else None
    else:
        row = execute_dynamic_query(conn, queries["find_tran_source_loc_301"], {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
        loc = row[0] if row else None
    return loc


def _do_unpick_engine(engine, wh_id, order_number, item_number, run_id, qty=None):
    """Full or partial unpick using a SQLAlchemy engine."""
    log_unpick("INFO", f"Processing started for order {order_number}, item {item_number}, warehouse {wh_id}.",
               order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
    queries = load_agent_queries("unpick", DEFAULT_QUERIES)
    try:
        with engine.begin() as conn:
            # Step 1: Check picked quantity
            qty_row = execute_dynamic_query(conn, queries["find_picked_qty"], {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
            if not qty_row or not qty_row[0] or float(qty_row[0]) <= 0:
                msg = "picked_quantity is 0 or NULL — skipping."
                log_unpick("WARNING", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
                return {"status": "WARNING", "message": msg}

            actual = float(qty_row[0])
            q = qty if qty is not None else actual
            log_unpick("INFO", f"Step 1: Found picked_quantity = {actual}, unpick_qty = {q}.",
                       order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            if qty and qty > actual:
                msg = f"unpick_qty ({qty}) exceeds picked_quantity ({actual})."
                log_unpick("WARNING", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
                return {"status": "WARNING", "message": msg}

            # Step 2: Resolve pick location
            pick_loc = _resolve_pick_loc_conn(conn, wh_id, order_number, item_number, queries)
            if not pick_loc:
                msg = "Could not resolve pick_location — skipping."
                log_unpick("WARNING", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
                return {"status": "WARNING", "message": msg}
            log_unpick("INFO", f"Step 2: Resolved pick location = {pick_loc}.",
                       order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

            # Step 3: Update pick detail (unstage & reduce picked qty)
            execute_dynamic_query(conn, queries["update_pick_detail"], {"q": q, "w": wh_id, "o": order_number, "i": item_number})
            log_unpick("INFO", f"Step 3: Updated t_pick_detail — reduced picked_quantity by {q}, adjusted staged_quantity and status.",
                       order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

            # Step 4: Inventory restoration based on item_hu_indicator
            loc_row = execute_dynamic_query(conn, queries["check_item_indicator"], {"w": wh_id, "l": pick_loc}).fetchone()
            ihi = loc_row[0] if loc_row else None

            if ihi in ('I', 'H'):
                si = execute_dynamic_query(conn, queries["check_stored_item"], {"w": wh_id, "i": item_number, "l": pick_loc}).fetchone()
                if si:
                    execute_dynamic_query(conn, queries["update_stored_qty_add"], {"q": q, "w": wh_id, "i": item_number, "l": pick_loc})
                    log_unpick("INFO", f"Step 4a: Existing STORAGE record found at {pick_loc} — added {q} to actual_qty.",
                               order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
                else:
                    execute_dynamic_query(conn, queries["update_stored_qty_move"], {"l": pick_loc, "q": q, "w": wh_id, "i": item_number, "o": order_number})
                    log_unpick("INFO", f"Step 4a: No STORAGE record at {pick_loc} — moved order-type record back to STORAGE.",
                               order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
                execute_dynamic_query(conn, queries["update_stored_qty_sub"], {"q": q, "w": wh_id, "i": item_number, "o": order_number})
                execute_dynamic_query(conn, queries["delete_empty_stored_item"], {"w": wh_id, "i": item_number, "o": order_number})
                log_unpick("INFO", f"Step 4b: Reduced order-type stored_item qty by {q} and cleaned up empty records.",
                           order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

                hu_row = execute_dynamic_query(conn, queries["find_hu_id"], {"w": wh_id, "i": item_number, "o": order_number}).fetchone()
                if hu_row:
                    h = hu_row[0]
                    execute_dynamic_query(conn, queries["update_hu_qty_sub"], {"q": q, "w": wh_id, "h": h, "i": item_number, "o": order_number})
                    execute_dynamic_query(conn, queries["delete_empty_hu_detail"], {"w": wh_id, "h": h, "i": item_number, "o": order_number})
                    log_unpick("INFO", f"Step 4c: HU {h} — reduced hu_detail qty by {q} and cleaned up empty detail.",
                               order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
                    if not execute_dynamic_query(conn, queries["check_hu_detail"], {"w": wh_id, "h": h}).fetchone():
                        execute_dynamic_query(conn, queries["delete_empty_hu_master"], {"w": wh_id, "h": h})
                        log_unpick("INFO", f"Step 4d: HU {h} has no remaining details — deleted hu_master record.",
                                   order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
                else:
                    log_unpick("INFO", "Step 4c: No HU records found for this order-type — HU cleanup skipped.",
                               order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

                log_unpick("INFO", f"Step 4 (Case {ihi}): Inventory restoration complete at location {pick_loc}.",
                           order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            else:
                log_unpick("WARNING", f"Unknown item_hu_indicator '{ihi}' — skipping HU cleanup.",
                           order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

            # Step 5: Update work queue
            execute_dynamic_query(conn, queries["update_work_q"], {"w": wh_id, "o": order_number, "i": item_number})
            log_unpick("INFO", "Step 5: Updated t_work_q status based on pick completion.",
                       order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

        log_unpick("INFO", "All steps committed successfully.",
                   order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
        return {"status": "SUCCESS", "message": "Unpick completed."}

    except Exception as exc:
        logger.exception("Unpick failed for order=%s item=%s: %s", order_number, item_number, exc)
        msg = f"All changes rolled back. Error: {exc}"
        log_unpick("ERROR", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
        return {"status": "ERROR", "message": msg}


# ── Dry-run preview (SQLAlchemy) ─────────────────────────────────────────────

def _dry_run_unpick_engine(engine, wh_id, order_number, item_number, qty=None):
    """Read-only preview of what _do_unpick_engine would do — no writes."""
    queries = load_agent_queries("unpick", DEFAULT_QUERIES)
    try:
        with engine.connect() as conn:
            qty_row = execute_dynamic_query(conn, queries["find_picked_qty"],
                                            {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
        if not qty_row or not qty_row[0] or float(qty_row[0]) <= 0:
            return {"status": "WARNING", "message": "picked_quantity is 0 or NULL — nothing to unpick.",
                    "preview": None, "steps": []}

        actual = float(qty_row[0])
        q = qty if qty is not None else actual
        if qty and qty > actual:
            return {"status": "WARNING", "message": f"unpick_qty ({qty}) exceeds picked_quantity ({actual}).",
                    "preview": None, "steps": []}

        with engine.connect() as conn:
            pick_loc = _resolve_pick_loc_conn(conn, wh_id, order_number, item_number, queries)

        steps = [
            f"Reduce picked_quantity by {q} (current: {actual})",
            f"Restore {q} unit(s) of {item_number} to pick location {pick_loc or '(unknown)'}",
            "Update work queue status",
        ]
        return {
            "status": "DRY_RUN",
            "message": f"Would unpick {q} unit(s) of {item_number} from order {order_number} (WH {wh_id})",
            "preview": {"wh_id": wh_id, "order_number": order_number, "item_number": item_number,
                        "picked_quantity": actual, "unpick_quantity": q, "pick_location": pick_loc},
            "steps": steps,
        }
    except Exception as exc:
        return {"status": "ERROR", "message": f"Dry-run check failed: {exc}", "preview": None, "steps": []}


@bp.route("/api/v0/unpick_agent/dry_run", methods=["POST"])
@require_agent("unpick")
@limiter.limit("10 per minute")
def unpick_dry_run():
    data         = request.get_json() or {}
    db_config_id = data.get("db_config_id", "").strip()
    records      = data.get("records", [])
    if not db_config_id:
        return jsonify({"type": "error", "error": "db_config_id is required"}), 400
    if not records:
        return jsonify({"type": "error", "error": "No records provided"}), 400
    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404

    engine  = get_engine(db_config_id, cfg)
    results = []
    for rec in records:
        wh_id        = str(rec.get("wh_id", "")).strip()
        order_number = str(rec.get("order_number", "")).strip()
        item_number  = str(rec.get("item_number", "")).strip()
        if not wh_id or not order_number or not item_number:
            results.append({"wh_id": wh_id, "order_number": order_number, "item_number": item_number,
                            "status": "WARNING", "message": "Missing required field(s).", "steps": []})
            continue
        res = _dry_run_unpick_engine(engine, wh_id, order_number, item_number)
        results.append({"wh_id": wh_id, "order_number": order_number, "item_number": item_number, **res})
    return jsonify({"type": "success", "results": results, "dry_run": True})


# ── Shared unpick logic (pyodbc cursor) ──────────────────────────────────────

def _resolve_pick_loc_cursor(cursor, wh_id, order_number, item_number, queries):
    execute_dynamic_query(cursor, queries["check_columns"], {})
    if cursor.fetchone():
        row = execute_dynamic_query(cursor, queries["find_pick_loc"], {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
        loc = row[0] if row else None
        if not loc:
            row2 = execute_dynamic_query(cursor, queries["find_tran_loc_301"], {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
            loc = row2[0] if row2 else None
    else:
        row = execute_dynamic_query(cursor, queries["find_tran_source_loc_301"], {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
        loc = row[0] if row else None
    return loc


def _do_unpick_pyodbc(conn, wh_id, order_number, item_number, run_id, qty=None):
    """Full or partial unpick using a pyodbc connection."""
    log_unpick("INFO", f"Processing started for order {order_number}, item {item_number}, warehouse {wh_id}.",
               order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
    cursor = conn.cursor()
    queries = load_agent_queries("unpick", DEFAULT_QUERIES)
    try:
        # Step 1: Check picked quantity
        qty_row = execute_dynamic_query(cursor, queries["find_picked_qty"], {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
        if not qty_row or not qty_row[0] or float(qty_row[0]) <= 0:
            msg = "picked_quantity is 0 or NULL — nothing to unpick."
            log_unpick("WARNING", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            conn.rollback()
            return {"status": "WARNING", "message": msg}

        actual = float(qty_row[0])
        q = qty if qty is not None else actual
        log_unpick("INFO", f"Step 1: Found picked_quantity = {actual}, unpick_qty = {q}.",
                   order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
        if qty and qty > actual:
            msg = f"unpick_qty ({qty}) exceeds picked_quantity ({actual})."
            log_unpick("WARNING", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            conn.rollback()
            return {"status": "WARNING", "message": msg}

        # Step 2: Resolve pick location
        pick_loc = _resolve_pick_loc_cursor(cursor, wh_id, order_number, item_number, queries)
        if not pick_loc:
            msg = "Could not resolve pick_location — skipping."
            log_unpick("WARNING", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            conn.rollback()
            return {"status": "WARNING", "message": msg}
        log_unpick("INFO", f"Step 2: Resolved pick location = {pick_loc}.",
                   order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

        # Step 3: Update pick detail
        execute_dynamic_query(cursor, queries["update_pick_detail"], {"q": q, "w": wh_id, "o": order_number, "i": item_number})
        log_unpick("INFO", f"Step 3: Updated t_pick_detail — {cursor.rowcount} row(s), reduced picked_quantity by {q}.",
                   order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

        # Step 4: Inventory restoration
        loc_row = execute_dynamic_query(cursor, queries["check_item_indicator"], {"w": wh_id, "l": pick_loc}).fetchone()
        ihi = loc_row[0] if loc_row else None

        if ihi in ('I', 'H'):
            si = execute_dynamic_query(cursor, queries["check_stored_item"], {"w": wh_id, "i": item_number, "l": pick_loc}).fetchone()
            if si:
                execute_dynamic_query(cursor, queries["update_stored_qty_add"], {"q": q, "w": wh_id, "i": item_number, "l": pick_loc})
                log_unpick("INFO", f"Step 4a: Existing STORAGE record found at {pick_loc} — added {q} to actual_qty.",
                           order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            else:
                execute_dynamic_query(cursor, queries["update_stored_qty_move"], {"l": pick_loc, "q": q, "w": wh_id, "i": item_number, "o": order_number})
                log_unpick("INFO", f"Step 4a: No STORAGE record at {pick_loc} — moved order-type record back to STORAGE.",
                           order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            execute_dynamic_query(cursor, queries["update_stored_qty_sub"], {"q": q, "w": wh_id, "i": item_number, "o": order_number})
            execute_dynamic_query(cursor, queries["delete_empty_stored_item"], {"w": wh_id, "i": item_number, "o": order_number})
            log_unpick("INFO", f"Step 4b: Reduced order-type stored_item qty by {q} and cleaned up empty records.",
                       order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

            hu_row = execute_dynamic_query(cursor, queries["find_hu_id"], {"w": wh_id, "i": item_number, "o": order_number}).fetchone()
            if hu_row:
                h = hu_row[0]
                execute_dynamic_query(cursor, queries["update_hu_qty_sub"], {"q": q, "w": wh_id, "h": h, "i": item_number, "o": order_number})
                execute_dynamic_query(cursor, queries["delete_empty_hu_detail"], {"w": wh_id, "h": h, "i": item_number, "o": order_number})
                log_unpick("INFO", f"Step 4c: HU {h} — reduced hu_detail qty by {q} and cleaned up empty detail.",
                           order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
                if not execute_dynamic_query(cursor, queries["check_hu_detail"], {"w": wh_id, "h": h}).fetchone():
                    execute_dynamic_query(cursor, queries["delete_empty_hu_master"], {"w": wh_id, "h": h})
                    log_unpick("INFO", f"Step 4d: HU {h} has no remaining details — deleted hu_master record.",
                               order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            else:
                log_unpick("INFO", "Step 4c: No HU records found for this order-type — HU cleanup skipped.",
                           order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
            log_unpick("INFO", f"Step 4 (Case {ihi}): Inventory restoration complete at location {pick_loc}.",
                       order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
        else:
            log_unpick("WARNING", f"Unknown indicator '{ihi}' — skipping HU.",
                       order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

        # Step 5: Update work queue
        execute_dynamic_query(cursor, queries["update_work_q"], {"w": wh_id, "o": order_number, "i": item_number})
        log_unpick("INFO", f"Step 5: Updated t_work_q — {cursor.rowcount} row(s) updated.",
                   order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)

        conn.commit(); cursor.close()
        log_unpick("INFO", "All steps committed successfully.",
                   order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
        return {"status": "SUCCESS", "message": "Unpick completed."}

    except Exception as exc:
        cursor.close()
        try: conn.rollback()
        except Exception: pass
        msg = f"All changes rolled back. Error: {exc}"
        logger.exception("Unpick failed: %s", exc)
        log_unpick("ERROR", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
        return {"status": "ERROR", "message": msg}


# ── Scheduler controls ────────────────────────────────────────────────────────

@bp.route("/api/v0/unpick_agent/scheduler_status", methods=["GET"])
@require_agent("unpick")
def scheduler_status():
    from scheduler import unpick_job_info
    return jsonify(unpick_job_info())


@bp.route("/api/v0/unpick_agent/scheduler_toggle", methods=["POST"])
@admin_required
def scheduler_toggle():
    from scheduler import get_unpick_scheduler
    s = get_unpick_scheduler()
    if not s:
        return jsonify({"type": "error", "error": "Scheduler not initialized"}), 400
    job = s.get_job("auto_unpick")
    if not job:
        return jsonify({"type": "error", "error": "Job not found"}), 400
    if job.next_run_time is None:
        s.resume_job("auto_unpick"); action = "resumed"
    else:
        s.pause_job("auto_unpick"); action = "paused"
    log_audit_action(current_user.username, "SCHEDULER_TOGGLE", "unpick", {"action": action})
    return jsonify({"type": "success", "action": action})


@bp.route("/api/v0/unpick_agent/scheduler_interval", methods=["POST"])
@admin_required
def scheduler_interval():
    from scheduler import get_unpick_scheduler
    s = get_unpick_scheduler()
    if not s:
        return jsonify({"type": "error", "error": "Scheduler not initialized"}), 400
    data = request.get_json() or {}
    try:
        hours = float(data.get("hours", 0))
        if not (0.25 <= hours <= 168):
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"type": "error", "error": "hours must be between 0.25 and 168"}), 400
    s.reschedule_job("auto_unpick", trigger="interval", hours=hours)
    log_audit_action(current_user.username, "SCHEDULER_INTERVAL", "unpick", {"hours": hours})
    return jsonify({"type": "success", "hours": hours})


# ── Auto scan ─────────────────────────────────────────────────────────────────

@bp.route("/api/v0/unpick_agent/auto_scan", methods=["POST"])
@require_agent("unpick")
def auto_scan():
    data = request.get_json() or {}
    db_config_id = data.get("db_config_id", "").strip()
    if not db_config_id:
        return jsonify({"type": "error", "error": "db_config_id is required"}), 400
    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404
    conn = None
    try:
        conn = get_engine(db_config_id, cfg).raw_connection()
        conn.autocommit = False
        cursor = conn.cursor()
        queries = load_agent_queries("unpick", DEFAULT_QUERIES)
        execute_dynamic_query(cursor, queries["auto_scan"], {})
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return jsonify({"type": "success", "records": rows, "count": len(rows)})
    except Exception as e:
        logger.error("Auto scan failed: %s", e, exc_info=True)
        return jsonify({"type": "error", "error": str(e)}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


# ── Manual full execute ───────────────────────────────────────────────────────

@bp.route("/api/v0/unpick_agent/execute", methods=["POST"])
@require_agent("unpick")
@limiter.limit("10 per minute")
def execute():
    data         = request.get_json() or {}
    db_config_id = data.get("db_config_id", "").strip()
    records      = data.get("records", [])
    if not db_config_id:
        return jsonify({"type": "error", "error": "db_config_id is required"}), 400
    if not records:
        return jsonify({"type": "error", "error": "No records provided"}), 400
    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404

    run_id = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    log_unpick("INFO", f"Manual unpick run started. {len(records)} record(s).", run_id=run_id)
    results = []

    try:
        for rec in records:
            wh_id        = str(rec.get("wh_id", "")).strip()
            order_number = str(rec.get("order_number", "")).strip()
            item_number  = str(rec.get("item_number", "")).strip()
            if not wh_id or not order_number or not item_number:
                msg = "Skipped — missing required field(s)."
                results.append({"wh_id": wh_id, "order_number": order_number, "item_number": item_number,
                                "status": "WARNING", "message": msg})
                continue
            conn = None
            try:
                conn = get_engine(db_config_id, cfg).raw_connection()
                conn.autocommit = False
                result = _do_unpick_pyodbc(conn, wh_id, order_number, item_number, run_id)
            except Exception as exc:
                if conn:
                    try: conn.rollback()
                    except Exception: pass
                result = {"status": "ERROR", "message": str(exc)}
            finally:
                if conn:
                    try: conn.close()
                    except Exception: pass
            results.append({"wh_id": wh_id, "order_number": order_number, "item_number": item_number, **result})

        notify.send_run_report(db_config_id, "Unpick Agent", results, current_user.display_name or current_user.username)
        log_unpick("INFO", f"Manual run completed. {len(results)} record(s) processed.", run_id=run_id)
        log_audit_action(current_user.username, "EXECUTE_AUTO_SCAN_UNPICK", db_config_id, {"records_count": len(records), "results": results})
        return jsonify({"type": "success", "results": results, "run_id": run_id})
    finally:
        from log_buffers import flush_logs_to_db
        flush_logs_to_db(run_id, "unpick")


# ── Manual full unpick ────────────────────────────────────────────────────────

def _do_manual_unpick_pyodbc(conn, wh_id, order_number, item_number, run_id):
    """Full manual unpick using a pyodbc connection following the exact 3-step SQL flow."""
    _kw = dict(order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
    log_unpick("INFO", "Processing started (manual full unpick).", **_kw)
    cursor = conn.cursor()
    queries = load_agent_queries("unpick", DEFAULT_QUERIES)
    try:
        # STEP 1: UNSTAGE & UNPICK
        execute_dynamic_query(cursor, queries["manual_unpick_update_pick"], {"o": order_number, "w": wh_id, "i": item_number})
        log_unpick("INFO",
                   f"Step 1 (t_pick_detail): Set picked_quantity=0, staged_quantity=0, status=RELEASED — {cursor.rowcount} row(s) updated.",
                   **_kw)

        # STEP 2: RESOLVE PICK LOCATION
        pick_loc = _resolve_pick_loc_cursor(cursor, wh_id, order_number, item_number, queries)
        if not pick_loc:
            msg = "Could not resolve pick_location."
            log_unpick("WARNING", msg, **_kw)
            conn.rollback()
            return {"status": "WARNING", "message": msg}
        log_unpick("INFO", f"Step 2: Resolved pick location: {pick_loc}.", **_kw)

        # GET ITEM HU INDICATOR
        row = execute_dynamic_query(cursor, queries["check_item_indicator"], {"w": wh_id, "l": pick_loc}).fetchone()
        ihi = row[0] if row else None
        log_unpick("INFO", f"Item HU indicator at {pick_loc}: {ihi!r}.", **_kw)

        if ihi == 'I':
            # GET PICKED HU ID
            row = execute_dynamic_query(cursor, queries["find_hu_id"], {"w": wh_id, "i": item_number, "o": order_number}).fetchone()
            picked_hu_id = row[0] if row else None
            log_unpick("INFO", f"Step 2 (Case I): Picked HU ID: {picked_hu_id or 'none'}.", **_kw)

            # RESTORE STORED ITEM
            exists_stored = execute_dynamic_query(cursor, queries["check_stored_item"], {"w": wh_id, "i": item_number, "l": pick_loc}).fetchone()
            if exists_stored:
                execute_dynamic_query(cursor, queries["manual_update_stored_qty_add"], {"w": wh_id, "i": item_number, "l": pick_loc, "o": order_number})
                execute_dynamic_query(cursor, queries["manual_delete_stored_item"], {"w": wh_id, "i": item_number, "o": order_number})
                log_unpick("INFO", f"Step 2a (Case I): Existing STORAGE record at {pick_loc} — merged qty and deleted order-type record.", **_kw)
            else:
                execute_dynamic_query(cursor, queries["manual_update_stored_item_move"], {"l": pick_loc, "w": wh_id, "i": item_number, "o": order_number})
                log_unpick("INFO", f"Step 2a (Case I): No STORAGE record at {pick_loc} — moved order-type record to STORAGE.", **_kw)

            # DELETE FROM HU DETAIL & MASTER
            if picked_hu_id:
                execute_dynamic_query(cursor, queries["manual_delete_hu_detail"], {"w": wh_id, "h": picked_hu_id, "i": item_number, "o": order_number})
                log_unpick("INFO", f"Step 2b (Case I): HU detail for LP {picked_hu_id} deleted.", **_kw)
                row = execute_dynamic_query(cursor, queries["check_hu_detail"], {"w": wh_id, "h": picked_hu_id}).fetchone()
                if not row:
                    execute_dynamic_query(cursor, queries["delete_empty_hu_master"], {"w": wh_id, "h": picked_hu_id})
                    log_unpick("INFO", f"Step 2c (Case I): HU master {picked_hu_id} removed (no remaining detail lines).", **_kw)
                else:
                    log_unpick("INFO", f"Step 2c (Case I): HU master {picked_hu_id} kept (other detail lines remain).", **_kw)
            else:
                log_unpick("INFO", "Step 2b (Case I): No HU ID found — HU cleanup skipped.", **_kw)

        elif ihi == 'H':
            # GET PICKED HU ID
            row = execute_dynamic_query(cursor, queries["find_hu_id"], {"w": wh_id, "i": item_number, "o": order_number}).fetchone()
            picked_hu_id = row[0] if row else None

            # GET DISTINCT ITEM COUNT
            item_count = 0
            if picked_hu_id:
                row = execute_dynamic_query(cursor, queries["manual_get_distinct_item_count"], {"w": wh_id, "h": picked_hu_id}).fetchone()
                item_count = row[0] if row else 0
            log_unpick("INFO", f"Step 2 (Case H): Picked HU ID: {picked_hu_id or 'none'}, distinct item count on LP: {item_count}.", **_kw)

            if item_count == 1:
                # SINGLE ITEM LP — clean up old LP, restore inventory
                execute_dynamic_query(cursor, queries["manual_delete_hu_detail"], {"w": wh_id, "h": picked_hu_id, "i": item_number, "o": order_number})
                log_unpick("INFO", f"Step 2a (Case H - Single LP): HU detail for LP {picked_hu_id} deleted.", **_kw)

                row = execute_dynamic_query(cursor, queries["check_hu_detail"], {"w": wh_id, "h": picked_hu_id}).fetchone()
                if not row:
                    execute_dynamic_query(cursor, queries["delete_empty_hu_master"], {"w": wh_id, "h": picked_hu_id})
                    log_unpick("INFO", f"Step 2b (Case H - Single LP): HU master {picked_hu_id} removed (empty).", **_kw)

                exists_stored = execute_dynamic_query(cursor, queries["check_stored_item"], {"w": wh_id, "i": item_number, "l": pick_loc}).fetchone()
                if exists_stored:
                    execute_dynamic_query(cursor, queries["manual_update_stored_qty_add"], {"w": wh_id, "i": item_number, "l": pick_loc, "o": order_number})
                    execute_dynamic_query(cursor, queries["manual_delete_stored_item"], {"w": wh_id, "i": item_number, "o": order_number})
                    log_unpick("INFO", f"Step 2c (Case H - Single LP): Existing STORAGE at {pick_loc} — merged qty and deleted order-type record.", **_kw)
                else:
                    execute_dynamic_query(cursor, queries["manual_update_stored_item_move"], {"l": pick_loc, "w": wh_id, "i": item_number, "o": order_number})
                    log_unpick("INFO", f"Step 2c (Case H - Single LP): No STORAGE at {pick_loc} — moved order-type record to STORAGE.", **_kw)

            elif item_count > 1:
                # MULTI ITEM LP — create new LP, reassign item, restore inventory
                import random
                rand_num = random.randint(0, 99999999)
                new_hu_id = f"UP{rand_num:08d}"

                execute_dynamic_query(cursor, queries["manual_insert_hu_master"], {"h": new_hu_id, "l": pick_loc, "w": wh_id})
                log_unpick("INFO", f"Step 2a (Case H - Multi LP): Created new LP {new_hu_id} at location {pick_loc}.", **_kw)

                execute_dynamic_query(cursor, queries["manual_update_hu_detail_multi"], {"nh": new_hu_id, "l": pick_loc, "w": wh_id, "oh": picked_hu_id, "i": item_number})
                log_unpick("INFO", f"Step 2b (Case H - Multi LP): HU detail for item {item_number} moved from LP {picked_hu_id} to new LP {new_hu_id}.", **_kw)

                exists_stored = execute_dynamic_query(cursor, queries["check_stored_item"], {"w": wh_id, "i": item_number, "l": pick_loc}).fetchone()
                if exists_stored:
                    execute_dynamic_query(cursor, queries["manual_update_stored_qty_add"], {"w": wh_id, "i": item_number, "l": pick_loc, "o": order_number})
                    execute_dynamic_query(cursor, queries["manual_delete_stored_item"], {"w": wh_id, "i": item_number, "o": order_number})
                    log_unpick("INFO", f"Step 2c (Case H - Multi LP): Existing STORAGE at {pick_loc} — merged qty and deleted order-type record.", **_kw)
                else:
                    execute_dynamic_query(cursor, queries["manual_update_stored_item_move"], {"l": pick_loc, "w": wh_id, "i": item_number, "o": order_number})
                    log_unpick("INFO", f"Step 2c (Case H - Multi LP): No STORAGE at {pick_loc} — moved order-type record to STORAGE.", **_kw)
            else:
                log_unpick("WARNING", f"Step 2 (Case H): No HU or item count is 0 — HU cleanup and inventory restore skipped.", **_kw)

        else:
            log_unpick("WARNING", f"Unknown item_hu_indicator '{ihi}' — skipping Step 2 inventory updates.", **_kw)

        # STEP 3: UPDATE WORK QUEUE
        execute_dynamic_query(cursor, queries["manual_update_work_q"], {"o": order_number, "w": wh_id, "i": item_number})
        log_unpick("INFO", f"Step 3 (t_work_q): Set work_status='U' — {cursor.rowcount} row(s) updated.", **_kw)

        conn.commit()
        cursor.close()
        log_unpick("INFO", "All steps committed successfully.", **_kw)
        return {"status": "SUCCESS", "message": "Manual unpick completed successfully."}

    except Exception as exc:
        cursor.close()
        try:
            conn.rollback()
        except Exception:
            pass
        msg = f"All changes rolled back. Error: {exc}"
        logger.exception("Manual unpick failed: %s", exc)
        log_unpick("ERROR", msg, **_kw)
        return {"status": "ERROR", "message": msg}


@bp.route("/api/v0/unpick_agent/manual_unpick", methods=["POST"])
@require_agent("unpick")
@limiter.limit("10 per minute")
def manual_unpick():
    data         = request.get_json() or {}
    db_config_id = data.get("db_config_id", "").strip()
    wh_id        = str(data.get("wh_id", "")).strip()
    order_number = str(data.get("order_number", "")).strip()
    item_number  = str(data.get("item_number", "")).strip()

    if not db_config_id:
        return jsonify({"type": "error", "error": "db_config_id is required"}), 400
    if not wh_id or not order_number or not item_number:
        return jsonify({"type": "error", "error": "wh_id, order_number, item_number required"}), 400

    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404

    run_id = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    conn = None
    try:
        conn = get_engine(db_config_id, cfg).raw_connection()
        conn.autocommit = False
        result = _do_manual_unpick_pyodbc(conn, wh_id, order_number, item_number, run_id)
        
        log_audit_action(
            current_user.username,
            "EXECUTE_MANUAL_UNPICK",
            order_number,
            {"db_config_id": db_config_id, "wh_id": wh_id, "item_number": item_number, "status": result["status"], "message": result.get("message")}
        )
        
        if result["status"] == "SUCCESS":
            notify.send_run_report(db_config_id, "Unpick Agent", [result], current_user.username)
            return jsonify({"type": "success", "message": result["message"], "run_id": run_id})
        if result["status"] == "WARNING":
            return jsonify({"type": "warning", "message": result["message"]})
        return jsonify({"type": "error", "message": result["message"]}), 500
    except Exception as exc:
        if conn:
            try: conn.rollback()
            except Exception: pass
        msg = f"All changes rolled back. Error: {exc}"
        logger.exception("Manual unpick failed: %s", exc)
        log_unpick("ERROR", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
        log_audit_action(
            current_user.username,
            "EXECUTE_MANUAL_UNPICK",
            order_number,
            {"db_config_id": db_config_id, "wh_id": wh_id, "item_number": item_number, "status": "ERROR", "message": msg}
        )
        return jsonify({"type": "error", "message": msg}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
        from log_buffers import flush_logs_to_db
        flush_logs_to_db(run_id, "unpick")


# ── Partial unpick ────────────────────────────────────────────────────────────

@bp.route("/api/v0/unpick_agent/partial_unpick", methods=["POST"])
@require_agent("unpick")
@limiter.limit("10 per minute")
def partial_unpick():
    data           = request.get_json() or {}
    db_config_id   = data.get("db_config_id", "").strip()
    wh_id          = str(data.get("wh_id", "")).strip()
    order_number   = str(data.get("order_number", "")).strip()
    item_number    = str(data.get("item_number", "")).strip()
    unpick_qty_raw = data.get("unpick_qty")

    if not db_config_id:
        return jsonify({"type": "error", "error": "db_config_id is required"}), 400
    if not wh_id or not order_number or not item_number:
        return jsonify({"type": "error", "error": "wh_id, order_number, item_number required"}), 400
    try:
        unpick_qty = float(unpick_qty_raw)
        if unpick_qty <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"type": "error", "error": "unpick_qty must be a positive number"}), 400

    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404

    run_id = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    conn = None
    try:
        conn = get_engine(db_config_id, cfg).raw_connection()
        conn.autocommit = False
        result = _do_unpick_pyodbc(conn, wh_id, order_number, item_number, run_id, qty=unpick_qty)
        
        log_audit_action(
            current_user.username,
            "EXECUTE_PARTIAL_UNPICK",
            order_number,
            {"db_config_id": db_config_id, "wh_id": wh_id, "item_number": item_number, "unpick_qty": unpick_qty, "status": result["status"], "message": result.get("message")}
        )
        
        if result["status"] == "SUCCESS":
            notify.send_run_report(db_config_id, "Unpick Agent", [result], current_user.username)
            return jsonify({"type": "success", "message": result["message"], "run_id": run_id})
        if result["status"] == "WARNING":
            return jsonify({"type": "warning", "message": result["message"]})
        return jsonify({"type": "error", "message": result["message"]}), 500
    except Exception as exc:
        if conn:
            try: conn.rollback()
            except Exception: pass
        msg = f"All changes rolled back. Error: {exc}"
        logger.exception("Partial unpick failed: %s", exc)
        log_unpick("ERROR", msg, order_number=order_number, item_number=item_number, wh_id=wh_id, run_id=run_id)
        log_audit_action(
            current_user.username,
            "EXECUTE_PARTIAL_UNPICK",
            order_number,
            {"db_config_id": db_config_id, "wh_id": wh_id, "item_number": item_number, "unpick_qty": unpick_qty, "status": "ERROR", "message": msg}
        )
        return jsonify({"type": "error", "message": msg}), 500
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
        from log_buffers import flush_logs_to_db
        flush_logs_to_db(run_id, "unpick")


# ── Pick quantity lookup (I-04) ───────────────────────────────────────────────

@bp.route("/api/v0/unpick_agent/pick_qty", methods=["GET"])
@require_agent("unpick")
def pick_qty():
    db_config_id = request.args.get("db_config_id", "").strip()
    wh_id        = request.args.get("wh_id",        "").strip()
    order_number = request.args.get("order_number", "").strip()
    item_number  = request.args.get("item_number",  "").strip()
    if not db_config_id or not wh_id or not order_number or not item_number:
        return jsonify({"type": "error", "error": "db_config_id, wh_id, order_number, item_number required"}), 400
    cfg = get_config(db_config_id)
    if not cfg:
        return jsonify({"type": "error", "error": "DB config not found"}), 404
    try:
        conn    = get_engine(db_config_id, cfg).raw_connection()
        queries = load_agent_queries("unpick", DEFAULT_QUERIES)
        cursor  = conn.cursor()
        row = execute_dynamic_query(cursor, queries["find_picked_qty"],
                                    {"w": wh_id, "o": order_number, "i": item_number}).fetchone()
        cursor.close(); conn.close()
        qty = float(row[0]) if row and row[0] is not None else None
        return jsonify({"type": "success", "picked_quantity": qty})
    except Exception as e:
        return jsonify({"type": "error", "error": str(e)}), 500


# ── Logs ──────────────────────────────────────────────────────────────────────

@bp.route("/api/v0/unpick_agent/logs", methods=["GET"])
@require_agent("unpick")
def unpick_logs():
    from_dt = request.args.get("from", "").replace("T", " ")
    to_dt   = request.args.get("to",   "").replace("T", " ")
    if from_dt or to_dt:
        try:
            from auth import _get_conn
            conds  = ["log_type = ?"]
            params = ["unpick"]
            if from_dt: conds.append("timestamp >= ?"); params.append(from_dt)
            if to_dt:   conds.append("timestamp <= ?"); params.append(to_dt)
            conn = _get_conn()
            rows = conn.execute(
                "SELECT TOP 2000 run_id, timestamp, level, wh_id, order_number, item_number, message "
                f"FROM job_logs WHERE {' AND '.join(conds)} ORDER BY id DESC",
                params
            ).fetchall()
            conn.close()
            logs = [{"run_id": r[0], "timestamp": r[1], "level": r[2],
                     "wh_id": r[3], "order_number": r[4], "item_number": r[5], "message": r[6]}
                    for r in reversed(rows)]
            return jsonify({"logs": logs})
        except Exception as exc:
            logger.warning("unpick_logs date filter error: %s", exc)
    return jsonify({"logs": get_unpick_logs()})


@bp.route("/api/v0/unpick_agent/logs/download", methods=["GET"])
@require_agent("unpick")
def unpick_logs_download():
    fmt = request.args.get("format", "csv").lower()
    entries = get_unpick_logs()
    if fmt == "txt":
        lines = [
            "Unpick Agent Logs", "=" * 70,
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"Total entries: {len(entries)}", "",
        ]
        current_run = None
        for e in entries:
            if e["run_id"] != current_run:
                current_run = e["run_id"]
                lines += ["", f"Run: {current_run}", "-" * 50]
            rec = f"  [{e['wh_id']}] {e['order_number']} / {e['item_number']}  |  " if e.get("order_number") else "  "
            lines.append(f"  [{e['timestamp']}]  {e['level']:<9}{rec}{e['message']}")
        lines.append("")
        return Response("\n".join(lines), mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=unpick_logs.txt"})
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Run ID", "Timestamp", "Level", "WH ID", "Order Number", "Item Number", "Message"])
    for e in entries:
        writer.writerow([e["run_id"], e["timestamp"], e["level"], e["wh_id"],
                         e["order_number"], e["item_number"], e["message"]])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=unpick_logs.csv"})
