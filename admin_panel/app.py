"""
admin_panel/app.py
--------------------
ScholarSync Admin Panel — a small, deliberately narrow Flask app.

What it does:
  * Shows whether the scholarsync bot service is running (systemctl)
  * Shows the recent pipeline log, searchable/filterable
  * Lets the admin update the bot's .env values (masked, existing keys only)
  * Lets the admin restart the bot service after an env change
  * Documents the operational commands so routine checks don't need SSH

What it deliberately does NOT do:
  * No user database / signup — one admin account, set via set_password.py
  * No ability to add new .env keys, run arbitrary shell, or touch anything
    outside the ScholarSync project directory
  * No secret value is ever written to a log file, this app's own logs, or
    the audit trail — only key NAMES appear there

Run in production via Gunicorn behind Nginx (see deploy/), never
`python app.py` directly on the VM. See ADMIN_PANEL_GUIDE.md.
"""

import os
from datetime import timedelta
from functools import wraps

from dotenv import dotenv_values
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash

from admin_panel import env_editor, log_viewer, service_control, audit
from admin_panel.auth import AdminUser, throttle, verify_login

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_ENV_PATH = os.path.join(BASE_DIR, ".env")


def load_panel_config() -> dict:
    """Panel's OWN config (username/password hash/secret key) — separate .env
    from the bot's. Reloaded per-request so a password change takes effect
    immediately without a restart."""
    return dotenv_values(PANEL_ENV_PATH)


def create_app() -> Flask:
    app = Flask(__name__)
    config = load_panel_config()

    if not config.get("SECRET_KEY"):
        raise RuntimeError(
            "admin_panel/.env is missing SECRET_KEY. Run set_password.py first "
            "(see ADMIN_PANEL_GUIDE.md) before starting the panel."
        )

    app.config["SECRET_KEY"] = config["SECRET_KEY"]
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=int(config.get("SESSION_TIMEOUT_MINUTES", "120"))
    )
    # Cookies only ever travel over HTTPS, never readable by page JS, and
    # never sent cross-site. This matters a lot more here than on a normal
    # site, because the session behind that cookie can edit live credentials.
    app.config["SESSION_COOKIE_SECURE"] = config.get("BEHIND_HTTPS", "true").lower() != "false"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    CSRFProtect(app)

    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Please log in to continue."

    @login_manager.user_loader
    def load_user(username):
        cfg = load_panel_config()
        if username == cfg.get("ADMIN_USERNAME"):
            return AdminUser(username)
        return None

    scholarsync_root = config.get("SCHOLARSYNC_ROOT", os.path.dirname(BASE_DIR))
    service_name = config.get("SERVICE_NAME", "scholarsync")
    bot_env_path = os.path.join(scholarsync_root, ".env")
    bot_log_path = os.path.join(scholarsync_root, "scholarsync.log")

    def client_ip() -> str:
        # Trust X-Forwarded-For only because Nginx sits in front and sets it;
        # falls back to the direct connection if run standalone.
        forwarded = request.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "unknown")

    def log_action(action: str, detail: str = ""):
        audit.record(BASE_DIR, current_user.username if current_user.is_authenticated else "-",
                     client_ip(), action, detail)

    # ── Auth ─────────────────────────────────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        ip = client_ip()
        locked, remaining = throttle.is_locked(ip)
        if locked:
            flash(f"Too many failed attempts. Try again in {remaining // 60 + 1} minute(s).", "danger")
            return render_template("login.html", locked=True)

        if request.method == "POST":
            cfg = load_panel_config()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if verify_login(cfg, username, password):
                throttle.record_success(ip)
                session.permanent = True
                login_user(AdminUser(username))
                log_action("login_success")
                return redirect(url_for("dashboard"))
            throttle.record_failure(ip)
            log_action("login_failed", f"attempted_username={username}")
            flash("Invalid username or password.", "danger")

        return render_template("login.html", locked=False)

    @app.route("/logout")
    @login_required
    def logout():
        log_action("logout")
        logout_user()
        return redirect(url_for("login"))

    # ── Dashboard ────────────────────────────────────────────────────
    @app.route("/")
    @login_required
    def dashboard():
        status = service_control.status_summary(service_name)
        mem = service_control.process_memory_mb(status.get("main_pid", "0")) if status.get("ok") else None
        sysmem = service_control.system_memory()
        stats = log_viewer.quick_stats(bot_log_path)
        log_size_mb = log_viewer.file_size_mb(bot_log_path)
        return render_template(
            "dashboard.html",
            status=status, mem=mem, sysmem=sysmem, stats=stats, service_name=service_name,
            log_size_mb=log_size_mb,
        )

    @app.route("/restart", methods=["POST"])
    @login_required
    def restart_service():
        if request.form.get("confirm") != "yes":
            flash("Restart was not confirmed.", "warning")
            return redirect(url_for("dashboard"))
        ok, output = service_control.restart(service_name)
        log_action("restart_service", f"ok={ok}")
        flash("Service restart requested." if ok else f"Restart failed: {output}",
              "success" if ok else "danger")
        return redirect(url_for("dashboard"))

    # ── Logs ─────────────────────────────────────────────────────────
    @app.route("/logs")
    @login_required
    def logs():
        query = request.args.get("q", "")
        category = request.args.get("category", "")
        limit = min(int(request.args.get("limit", "300") or 300), 2000)
        results = log_viewer.search(bot_log_path, query=query, category=category, max_lines=limit)
        return render_template(
            "logs.html", results=results, query=query, category=category, limit=limit,
            categories=list(log_viewer.CATEGORIES.keys()), color=log_viewer.category_color,
        )

    # ── Archives (rotated .gz logs) ──────────────────────────────────
    def _known_archives():
        # Single source of truth for "what archives exist right now" —
        # also doubles as the allowlist for archive_view below.
        audit_log_path = os.path.join(BASE_DIR, "audit.log")
        return log_viewer.list_archives(bot_log_path, audit_log_path)

    @app.route("/archives")
    @login_required
    def archives():
        return render_template("archives.html", archives=_known_archives())

    @app.route("/archives/view")
    @login_required
    def archive_view():
        requested_name = request.args.get("file", "")
        # Allowlist match ONLY — never build a path from the request
        # directly. A name that isn't currently a real, freshly-listed
        # archive (expired, deleted, or just made up) is rejected outright.
        match = next((a for a in _known_archives() if a["name"] == requested_name), None)
        if not match:
            flash("That archive doesn't exist (it may have expired or already been deleted).", "warning")
            return redirect(url_for("archives"))

        query = request.args.get("q", "")
        category = request.args.get("category", "")
        limit = min(int(request.args.get("limit", "500") or 500), 3000)
        results = log_viewer.read_archive(match["path"], query=query, category=category, max_lines=limit)
        return render_template(
            "archive_view.html", archive=match, results=results, query=query, category=category, limit=limit,
            categories=list(log_viewer.CATEGORIES.keys()), color=log_viewer.category_color,
        )

    # ── Env editor ───────────────────────────────────────────────────
    @app.route("/env")
    @login_required
    def env_view():
        keys = env_editor.list_keys(bot_env_path)
        return render_template("env_editor.html", keys=keys)

    @app.route("/env/update", methods=["POST"])
    @login_required
    def env_update():
        updates = {}
        for field, value in request.form.items():
            if field.startswith("value_") and value.strip():
                if request.form.get(f"enable_{field[len('value_'):]}") == "on":
                    updates[field[len("value_"):]] = value.strip()

        if not updates:
            flash("No changes selected.", "warning")
            return redirect(url_for("env_view"))

        result = env_editor.update_values(bot_env_path, updates)
        log_action("env_update", f"keys={','.join(result['replaced'])}")

        if result["replaced"]:
            flash(
                f"Updated {len(result['replaced'])} key(s): {', '.join(result['replaced'])}. "
                "The bot won't see these until you restart the service.",
                "success",
            )
        if result["unknown"]:
            flash(f"Skipped unknown key(s): {', '.join(result['unknown'])}", "warning")

        return redirect(url_for("env_view"))

    # ── Commands reference ───────────────────────────────────────────
    @app.route("/commands")
    @login_required
    def commands():
        return render_template("commands.html", service_name=service_name)

    # ── Profile ──────────────────────────────────────────────────────
    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        cfg = load_panel_config()
        if request.method == "POST":
            current_password = request.form.get("current_password", "")
            new_username = request.form.get("new_username", "").strip()
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not verify_login(cfg, cfg.get("ADMIN_USERNAME", ""), current_password):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("profile"))

            updates = {}
            if new_username and new_username != cfg.get("ADMIN_USERNAME"):
                updates["ADMIN_USERNAME"] = new_username
            if new_password:
                if new_password != confirm_password:
                    flash("New password and confirmation don't match.", "danger")
                    return redirect(url_for("profile"))
                if len(new_password) < 12:
                    flash("New password must be at least 12 characters.", "danger")
                    return redirect(url_for("profile"))
                updates["ADMIN_PASSWORD_HASH"] = generate_password_hash(new_password)

            if updates:
                env_editor.update_values(PANEL_ENV_PATH, updates)
                log_action("profile_update", f"fields={','.join(updates.keys())}")
                flash("Profile updated. Please log in again.", "success")
                logout_user()
                return redirect(url_for("login"))

            flash("No changes submitted.", "warning")

        return render_template("profile.html", username=cfg.get("ADMIN_USERNAME", ""))

    @app.route("/audit")
    @login_required
    def audit_view():
        entries = audit.recent(BASE_DIR, max_lines=200)
        return render_template("audit.html", entries=entries)

    return app


# WSGI entrypoint used by Gunicorn: `gunicorn wsgi:app`
app = create_app()

if __name__ == "__main__":
    # Dev-only convenience run. NEVER use this in production — no HTTPS,
    # no Nginx IP allowlist. Real deployment is Gunicorn + Nginx, see
    # ADMIN_PANEL_GUIDE.md.
    app.run(host="127.0.0.1", port=8000, debug=False)
