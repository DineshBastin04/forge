import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_device_reset_scheduler = None
_unpick_scheduler = None

_JOB_DEFAULTS = {
    "max_instances":      1,
    "coalesce":           True,
    "misfire_grace_time": 300,
}


def get_metadata_db_engine():
    import os
    import urllib.parse
    from sqlalchemy import create_engine

    conn_str = os.getenv("METADATA_DB_CONN_STR")
    if not conn_str:
        server = os.getenv("METADATA_DB_SERVER", "localhost")
        database = os.getenv("METADATA_DB_DATABASE", "tychons_wi_agents")
        user = os.getenv("METADATA_DB_USER")
        password = os.getenv("METADATA_DB_PASSWORD")
        driver = os.getenv("METADATA_DB_DRIVER", "SQL Server")
        
        if user and password:
            conn_str = f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={user};PWD={password}"
        else:
            conn_str = f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};Trusted_Connection=yes;"

        if "ODBC Driver 18" in driver:
            conn_str += ";TrustServerCertificate=yes"

    quoted_conn = urllib.parse.quote_plus(conn_str)
    url = f"mssql+pyodbc:///?odbc_connect={quoted_conn}"
    
    pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=30,
        pool_pre_ping=True
    )


def start_schedulers(device_reset_fn, unpick_fn, device_hours: int = 2, unpick_hours: int = 2):
    global _device_reset_scheduler, _unpick_scheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    engine = get_metadata_db_engine()

    jobstores_dr = {
        "default": SQLAlchemyJobStore(engine=engine, tablename="device_reset_jobs")
    }
    jobstores_up = {
        "default": SQLAlchemyJobStore(engine=engine, tablename="unpick_jobs")
    }

    _device_reset_scheduler = BackgroundScheduler(
        jobstores=jobstores_dr,
        job_defaults=_JOB_DEFAULTS
    )
    _device_reset_scheduler.start()
    if not _device_reset_scheduler.get_job("identify_stuck_device"):
        _device_reset_scheduler.add_job(
            device_reset_fn, "interval",
            hours=device_hours,
            id="identify_stuck_device",
            name="Device Reset",
        )
    logger.info("Device Reset scheduler started — every %sh", device_hours)

    _unpick_scheduler = BackgroundScheduler(
        jobstores=jobstores_up,
        job_defaults=_JOB_DEFAULTS
    )
    _unpick_scheduler.start()
    if not _unpick_scheduler.get_job("auto_unpick"):
        _unpick_scheduler.add_job(
            unpick_fn, "interval",
            hours=unpick_hours,
            id="auto_unpick",
            name="Auto Unpick",
        )
    logger.info("Unpick scheduler started — every %sh", unpick_hours)


def shutdown_schedulers():
    for s in (_device_reset_scheduler, _unpick_scheduler):
        if s and s.running:
            try:
                s.shutdown(wait=False)
            except Exception:
                pass


def get_device_reset_scheduler():
    return _device_reset_scheduler


def get_unpick_scheduler():
    return _unpick_scheduler


def _job_info(sched, job_id: str) -> dict:
    if not sched or not sched.running:
        return {"running": False, "paused": True, "next_run": None, "interval_hours": 2}
    job = sched.get_job(job_id)
    if not job:
        return {"running": sched.running, "paused": True, "next_run": None, "interval_hours": 2}
    paused   = job.next_run_time is None
    next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else None
    try:
        interval_hours = job.trigger.interval.total_seconds() / 3600
    except AttributeError:
        interval_hours = 2
    return {
        "running":        sched.running,
        "paused":         paused,
        "next_run":       next_run,
        "interval_hours": interval_hours,
    }


def device_reset_job_info() -> dict:
    return _job_info(_device_reset_scheduler, "identify_stuck_device")


def unpick_job_info() -> dict:
    return _job_info(_unpick_scheduler, "auto_unpick")
