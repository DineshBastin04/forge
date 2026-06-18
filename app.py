"""
Tychons Wi-Agents — Flask application entry point.

Run:
    pip install -r requirements.txt
    cp .env.example .env   # fill in credentials
    python app.py
    -> http://localhost:5001
    -> Login: admin / change-me  (change immediately)
"""

import os
import atexit
import logging

from flask import Flask, send_from_directory, render_template, jsonify
from flask_login import login_required

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("tychons_wi_agents")


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.secret_key = os.getenv("SECRET_KEY", "change-me-before-production")

    # ── Session & Cookie Security ──────────────────────────────────────────────
    from datetime import timedelta
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=15)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Session cookies are always marked secure to guarantee XSS/CSRF security
    app.config["SESSION_COOKIE_SECURE"] = True

    # ── Flask-Login ───────────────────────────────────────────────────────────
    from auth import login_manager
    login_manager.init_app(app)
    login_manager.session_protection = "strong"

    @login_manager.unauthorized_handler
    def unauthorized():
        return jsonify({"type": "error", "error": "Not authenticated"}), 401

    # ── CSRF Protection ───────────────────────────────────────────────────────
    from flask_wtf.csrf import CSRFProtect, generate_csrf
    csrf = CSRFProtect(app)
    
    from auth import login as auth_login
    csrf.exempt(auth_login)

    @app.after_request
    def set_csrf_cookie(response):
        if response.status_code < 400:
            csrf_token = generate_csrf()
            response.set_cookie('csrf_token', csrf_token, samesite='Lax', secure=True)
        return response

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    from extensions import limiter
    limiter.init_app(app)

    @app.errorhandler(429)
    def rate_limit_exceeded(e):
        return jsonify({"type": "error", "error": f"Rate limit exceeded: {e.description}"}), 429

    # ── Blueprints ────────────────────────────────────────────────────────────
    from auth import bp as auth_bp
    from users import bp as users_bp
    from db_config import bp as db_config_bp
    from agents.device_reset import bp as dr_bp
    from agents.unpick import bp as unpick_bp

    for blueprint in (auth_bp, users_bp, db_config_bp, dr_bp, unpick_bp):
        app.register_blueprint(blueprint)

    # ── Health check ──────────────────────────────────────────────────────────
    @app.route("/api/v0/health")
    @login_required
    def health():
        from auth import _get_conn
        try:
            conn = _get_conn()
            conn.execute("SELECT 1")
            conn.close()
            metadata_db = "healthy"
        except Exception as e:
            metadata_db = f"unhealthy: {e}"

        import scheduler as sched
        schedulers = {
            "device_reset": sched.device_reset_job_info(),
            "unpick": sched.unpick_job_info()
        }

        from db import get_pool_status
        db_pools = get_pool_status()

        status = "healthy"
        if "unhealthy" in metadata_db:
            status = "unhealthy"

        return jsonify({
            "status": status,
            "metadata_db": metadata_db,
            "schedulers": schedulers,
            "db_pools": db_pools
        })

    # ── SPA shell ─────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/<path:path>")
    def catch_all(path):
        # Serve static files; fall back to SPA shell for client-side routes
        full = os.path.join(app.static_folder, path)
        if os.path.isfile(full):
            return send_from_directory(app.static_folder, path)
        return render_template("index.html")

    # ── Error handlers ────────────────────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"type": "error", "error": "Not found"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"type": "error", "error": "Internal server error"}), 500

    return app


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Init SQLite user store
    from auth import init_db
    init_db()

    # Start background schedulers
    try:
        from agents.device_reset import auto_job as dr_job
        from agents.unpick import auto_job as unpick_job
        import scheduler as sched

        device_hours = int(os.getenv("DEVICE_RESET_SCHEDULE_HOURS", "2"))
        unpick_hours = int(os.getenv("UNPICK_SCHEDULE_HOURS", "2"))
        sched.start_schedulers(dr_job, unpick_job, device_hours, unpick_hours)
        atexit.register(sched.shutdown_schedulers)
        logger.info("Schedulers started.")
    except Exception as e:
        logger.warning("Could not start schedulers: %s. Manual routes still available.", e)

    app = create_app()
    port  = int(os.getenv("FLASK_PORT", "5001"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    if debug:
        logger.info("FLASK_DEBUG=true — using Flask dev server on port %d", port)
        app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
    else:
        try:
            from waitress import serve
            
            # Production Startup Assertions
            assert app.secret_key != "change-me-before-production", (
                "Production Startup Error: SECRET_KEY must be changed from the default value in production."
            )
            assert os.getenv("DB_ENCRYPTION_KEY"), (
                "Production Startup Error: DB_ENCRYPTION_KEY must be configured in production."
            )
            
            threads = int(os.getenv("WAITRESS_THREADS", "8"))
            logger.info("Tychons Wi-Agents starting with Waitress — http://0.0.0.0:%d  (threads=%d)", port, threads)
            serve(app, host="0.0.0.0", port=port, threads=threads)
        except ImportError:
            logger.info("Waitress not found — using Flask dev server on port %d", port)
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
