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
import secrets
from datetime import timedelta
from functools import wraps

from dotenv import dotenv_values
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash

from admin_panel import (
    env_editor, log_viewer, service_control, audit, setup_state, wizard_state,
    telegram_login, category_editor,
)
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
        # Brand new deploy — nothing has ever been written to admin_panel/.env.
        # Rather than refuse to start (which would make the browser-based
        # /setup/claim page unreachable — the whole point of the self-service
        # wizard), auto-create a minimal file with just a fresh SECRET_KEY so
        # sessions/CSRF work. ADMIN_USERNAME / ADMIN_PASSWORD_HASH get added
        # moments later by /setup/claim (or set_password.py over SSH, if
        # preferred) — setup_state.is_admin_claimed() checks for those two
        # keys specifically, so this bootstrap-only state still correctly
        # reads as "not claimed yet".
        env_editor.bootstrap_panel_env(PANEL_ENV_PATH)
        config = load_panel_config()

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

    def wizard_token() -> str:
        # Not a secret — just a lookup key into wizard_state's server-side
        # dict. The actual values it points at (Udemy cookies, channel IDs,
        # the Telegram account name) never touch the browser cookie.
        token = session.get("wizard_token")
        if not token:
            token = secrets.token_urlsafe(16)
            session["wizard_token"] = token
        return token

    @app.context_processor
    def inject_setup_flags():
        # Cheap file-existence checks — lets base.html show a small
        # "finish setup" reminder banner on every page until the bot is
        # actually configured, without every route needing to pass it in.
        configured = setup_state.is_bot_configured(bot_env_path, scholarsync_root)
        return {"bot_configured": configured}

    # ── Auth ─────────────────────────────────────────────────────────
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        claimed = setup_state.is_admin_claimed(PANEL_ENV_PATH)

        ip = client_ip()
        locked, remaining = throttle.is_locked(ip)
        if locked:
            flash(f"Too many failed attempts. Try again in {remaining // 60 + 1} minute(s).", "danger")
            return render_template("login.html", locked=True, claimed=claimed)

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

        return render_template("login.html", locked=False, claimed=claimed)

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
        if not setup_state.is_bot_configured(bot_env_path, scholarsync_root):
            return redirect(url_for("setup_intro"))
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

    # ── Categories (keyword matrices + per-category min duration) ─────
    @app.route("/categories")
    @login_required
    def categories():
        policy = category_editor.load_policy(scholarsync_root)
        return render_template("categories.html", categories=policy["categories"],
                                other_min_hours=policy["other_min_hours"])

    @app.route("/categories/add", methods=["POST"])
    @login_required
    def categories_add():
        result = category_editor.add_category(
            scholarsync_root,
            request.form.get("name", ""),
            request.form.get("keywords", ""),
            request.form.get("min_hours", "8") or "8",
        )
        log_action("category_add", f"name={request.form.get('name','')} ok={result['ok']}")
        flash(result["error"] if not result["ok"] else "Category added. Restart the bot to apply it.",
              "danger" if not result["ok"] else "success")
        return redirect(url_for("categories"))

    @app.route("/categories/update/<name>", methods=["POST"])
    @login_required
    def categories_update(name):
        result = category_editor.update_category(
            scholarsync_root, name,
            request.form.get("keywords", ""),
            request.form.get("min_hours", "8") or "8",
        )
        log_action("category_update", f"name={name} ok={result['ok']}")
        flash(result["error"] if not result["ok"] else f"'{name}' updated. Restart the bot to apply it.",
              "danger" if not result["ok"] else "success")
        return redirect(url_for("categories"))

    @app.route("/categories/delete/<name>", methods=["POST"])
    @login_required
    def categories_delete(name):
        result = category_editor.delete_category(scholarsync_root, name)
        log_action("category_delete", f"name={name} ok={result['ok']}")
        flash(result["error"] if not result["ok"] else f"'{name}' deleted. Restart the bot to apply it.",
              "danger" if not result["ok"] else "success")
        return redirect(url_for("categories"))

    @app.route("/categories/other", methods=["POST"])
    @login_required
    def categories_other():
        try:
            hours = float(request.form.get("other_min_hours", "8"))
        except ValueError:
            flash("Enter a number for the fallback duration.", "danger")
            return redirect(url_for("categories"))
        category_editor.update_other_min_hours(scholarsync_root, hours)
        log_action("category_other_update", f"hours={hours}")
        flash("Fallback duration updated. Restart the bot to apply it.", "success")
        return redirect(url_for("categories"))

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

    # ── First-run setup wizard ───────────────────────────────────────
    # Lets a brand-new deploy be fully configured from a browser: create the
    # admin account, log in to Telegram (phone -> code -> optional 2FA),
    # pick target/alert channels by name instead of hunting numeric chat
    # IDs, paste Udemy cookies, then write the bot's .env and launch the
    # service — zero SSH once the code and system packages are on the VM.

    @app.route("/setup/claim", methods=["GET", "POST"])
    def setup_claim():
        # The ONLY page reachable before an admin account exists. The
        # instant ADMIN_USERNAME/ADMIN_PASSWORD_HASH are set, is_admin_claimed()
        # flips True and this permanently redirects to /login instead — it
        # can never be re-run to hijack an already-claimed instance.
        if setup_state.is_admin_claimed(PANEL_ENV_PATH):
            return redirect(url_for("login"))

        if request.method == "POST":
            username = request.form.get("username", "").strip() or "admin"
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            if len(password) < 12:
                flash("Password must be at least 12 characters.", "danger")
                return render_template("setup/claim.html", username=username)
            if password != confirm:
                flash("Passwords don't match.", "danger")
                return render_template("setup/claim.html", username=username)

            env_editor.append_or_update(PANEL_ENV_PATH, {
                "ADMIN_USERNAME": username,
                "ADMIN_PASSWORD_HASH": generate_password_hash(password),
                "SCHOLARSYNC_ROOT": os.path.dirname(BASE_DIR),
                "SERVICE_NAME": "scholarsync",
            })
            session.permanent = True
            login_user(AdminUser(username))
            log_action("admin_account_claimed")
            flash("Admin account created. Now let's connect the bot.", "success")
            return redirect(url_for("setup_intro"))

        return render_template("setup/claim.html", username="")

    @app.route("/setup")
    @login_required
    def setup_intro():
        if setup_state.is_bot_configured(bot_env_path, scholarsync_root):
            flash("Setup is already complete.", "info")
            return redirect(url_for("dashboard"))
        state = wizard_state.get(wizard_token())
        return render_template(
            "setup/intro.html",
            telegram_done=os.path.exists(setup_state.session_path(scholarsync_root)),
            channels_done=bool(state.get("target_channels")) and bool(state.get("alert_channel")),
            udemy_done=bool(state.get("udemy")),
        )

    @app.route("/setup/telegram", methods=["GET", "POST"])
    @login_required
    def setup_telegram():
        if request.method == "POST":
            raw_api_id = request.form.get("api_id", "").strip()
            api_hash = request.form.get("api_hash", "").strip()
            phone = request.form.get("phone", "").strip()
            try:
                api_id = int(raw_api_id)
            except ValueError:
                flash("API ID must be the plain number from my.telegram.org.", "danger")
                return render_template("setup/telegram_start.html")
            if not api_hash or not phone:
                flash("All three fields are required.", "danger")
                return render_template("setup/telegram_start.html")

            result = telegram_login.start_login(api_id, api_hash, phone, scholarsync_root)
            if not result["ok"]:
                flash(result["error"], "danger")
                return render_template("setup/telegram_start.html")

            wizard_state.update(wizard_token(), tg_token=result["token"], api_id=api_id,
                                 api_hash=api_hash, phone=phone)
            log_action("setup_telegram_code_sent")
            return redirect(url_for("setup_telegram_code"))

        return render_template("setup/telegram_start.html")

    @app.route("/setup/telegram/code", methods=["GET", "POST"])
    @login_required
    def setup_telegram_code():
        state = wizard_state.get(wizard_token())
        if not state.get("tg_token"):
            return redirect(url_for("setup_telegram"))

        if request.method == "POST":
            result = telegram_login.submit_code(state["tg_token"], request.form.get("code", ""))
            if not result["ok"]:
                flash(result["error"], "danger")
                return render_template("setup/telegram_code.html", phone=state.get("phone"))
            if result.get("needs_password"):
                return redirect(url_for("setup_telegram_password"))
            wizard_state.update(wizard_token(), tg_token=None, tg_name=result["name"],
                                 tg_username=result.get("username"))
            log_action("setup_telegram_login_success")
            flash(f"Logged in to Telegram as {result['name']}.", "success")
            return redirect(url_for("setup_channels"))

        return render_template("setup/telegram_code.html", phone=state.get("phone"))

    @app.route("/setup/telegram/password", methods=["GET", "POST"])
    @login_required
    def setup_telegram_password():
        state = wizard_state.get(wizard_token())
        if not state.get("tg_token"):
            return redirect(url_for("setup_telegram"))

        if request.method == "POST":
            result = telegram_login.submit_password(state["tg_token"], request.form.get("password", ""))
            if not result["ok"]:
                flash(result["error"], "danger")
                return render_template("setup/telegram_password.html")
            wizard_state.update(wizard_token(), tg_token=None, tg_name=result["name"],
                                 tg_username=result.get("username"))
            log_action("setup_telegram_login_success")
            flash(f"Logged in to Telegram as {result['name']}.", "success")
            return redirect(url_for("setup_channels"))

        return render_template("setup/telegram_password.html")

    @app.route("/setup/telegram/restart", methods=["POST"])
    @login_required
    def setup_telegram_restart():
        state = wizard_state.get(wizard_token())
        if state.get("tg_token"):
            telegram_login.cancel(state["tg_token"])
        wizard_state.update(wizard_token(), tg_token=None)
        flash("Login attempt cancelled — start again below.", "warning")
        return redirect(url_for("setup_telegram"))

    @app.route("/setup/channels", methods=["GET", "POST"])
    @login_required
    def setup_channels():
        state = wizard_state.get(wizard_token())
        if not os.path.exists(setup_state.session_path(scholarsync_root)):
            flash("Log in to Telegram first — channel lookup needs an authorized session.", "warning")
            return redirect(url_for("setup_telegram"))
        if not state.get("api_id") or not state.get("api_hash"):
            # Wizard progress expired (long gap since the Telegram step) even
            # though the session file itself is still valid — re-enter the
            # API credentials rather than a raw 500.
            flash("Your setup session timed out — re-enter your API ID/Hash to continue "
                  "(you're still logged in to Telegram, this won't ask for the code again "
                  "unless Telegram itself requires it).", "warning")
            return redirect(url_for("setup_telegram"))

        if request.method == "POST":
            action = request.form.get("action")
            identifier = request.form.get("identifier", "").strip()
            if identifier:
                result = telegram_login.resolve_channel(
                    state["api_id"], state["api_hash"], scholarsync_root, identifier
                )
                if not result["ok"]:
                    flash(result["error"], "danger")
                elif action == "add_target":
                    targets = state.get("target_channels", [])
                    if not any(t["id"] == result["id"] for t in targets):
                        targets.append({"id": result["id"], "title": result["title"]})
                        wizard_state.update(wizard_token(), target_channels=targets)
                    flash(f"Added {result['title']}.", "success")
                elif action == "set_alert":
                    wizard_state.update(wizard_token(),
                                         alert_channel={"id": result["id"], "title": result["title"]})
                    flash(f"Alert channel set to {result['title']}.", "success")
            return redirect(url_for("setup_channels"))

        return render_template("setup/channels.html", targets=state.get("target_channels", []),
                                alert=state.get("alert_channel"))

    @app.route("/setup/channels/remove/<int:chat_id>", methods=["POST"])
    @login_required
    def setup_channels_remove(chat_id):
        state = wizard_state.get(wizard_token())
        targets = [t for t in state.get("target_channels", []) if t["id"] != chat_id]
        wizard_state.update(wizard_token(), target_channels=targets)
        return redirect(url_for("setup_channels"))

    @app.route("/setup/udemy", methods=["GET", "POST"])
    @login_required
    def setup_udemy():
        state = wizard_state.get(wizard_token())
        if request.method == "POST":
            fields = ["access_token", "csrf_token", "dj_session_id", "user_jwt",
                      "client_id", "user_id", "cf_clearance", "cf_bm", "user_agent"]
            udemy = {f: request.form.get(f, "").strip() for f in fields}
            required = ["access_token", "csrf_token", "dj_session_id", "user_jwt"]
            missing = [f for f in required if not udemy[f]]
            if missing:
                flash(f"These cookies are required: {', '.join(missing)}.", "danger")
                return render_template("setup/udemy.html", values=udemy)
            wizard_state.update(wizard_token(), udemy=udemy)
            return redirect(url_for("setup_review"))
        return render_template("setup/udemy.html", values=state.get("udemy", {}))

    @app.route("/setup/review", methods=["GET", "POST"])
    @login_required
    def setup_review():
        state = wizard_state.get(wizard_token())
        targets = state.get("target_channels", [])
        alert = state.get("alert_channel")
        udemy = state.get("udemy")

        if request.method == "POST":
            if not targets or not alert or not udemy or not state.get("api_id") or not state.get("api_hash"):
                flash("Setup isn't complete yet — finish every step first.", "danger")
                return redirect(url_for("setup_intro"))

            def _clean(value: str) -> str:
                # A stray newline pasted into a cookie field could otherwise
                # inject an extra line (and therefore a new, unintended key)
                # into the .env file — strip line breaks defensively even
                # though this is the admin's own self-entered data.
                return (value or "").replace("\r", "").replace("\n", "").strip()

            values = {
                "API_ID": str(state["api_id"]),
                "API_HASH": _clean(state["api_hash"]),
                "TARGET_CHANNELS": ",".join(str(t["id"]) for t in targets),
                "ALERT_CHANNEL_ID": str(alert["id"]),
                "UDEMY_ACCESS_TOKEN": _clean(udemy["access_token"]),
                "UDEMY_CSRF_TOKEN": _clean(udemy["csrf_token"]),
                "UDEMY_DJ_SESSION_ID": _clean(udemy["dj_session_id"]),
                "UDEMY_USER_JWT": _clean(udemy["user_jwt"]),
                "UDEMY_CLIENT_ID": _clean(udemy.get("client_id", "")),
                "UDEMY_USER_ID": _clean(udemy.get("user_id", "")),
                "UDEMY_CF_CLEARANCE": _clean(udemy.get("cf_clearance", "")),
                "UDEMY_CF_BM": _clean(udemy.get("cf_bm", "")),
                "UDEMY_USER_AGENT": _clean(udemy.get("user_agent", "")),
            }
            env_editor.append_or_update(
                bot_env_path, values, header_comment="Written by the first-run setup wizard."
            )
            log_action("setup_env_written", f"keys={','.join(values.keys())}")

            ok, output = service_control.enable_and_start(service_name)
            log_action("setup_service_launch", f"ok={ok}")
            wizard_state.clear(wizard_token())

            if ok:
                flash("Setup complete — the bot is now running.", "success")
            else:
                flash(
                    f".env was written, but starting the service failed: {output}. "
                    "Check the Commands page / SSH in — the sudoers file may need the "
                    "enable/start lines from admin_panel/deploy/sudoers_scholarsync_panel.",
                    "warning",
                )
            return redirect(url_for("dashboard"))

        return render_template(
            "setup/review.html", targets=targets, alert=alert, udemy=udemy,
            tg_name=state.get("tg_name"), phone=state.get("phone"),
        )

    return app


# WSGI entrypoint used by Gunicorn: `gunicorn wsgi:app`
app = create_app()

if __name__ == "__main__":
    # Dev-only convenience run. NEVER use this in production — no HTTPS,
    # no Nginx IP allowlist. Real deployment is Gunicorn + Nginx, see
    # ADMIN_PANEL_GUIDE.md.
    app.run(host="127.0.0.1", port=8000, debug=False)
