"""
render_app.py
-------------
Render Web Service Entrypoint for UdemySync (24/7/365 Operation).

What this does on Render:
1. Starts a lightweight Flask Web Service on $PORT (required by Render to mark deploy healthy).
2. Runs the UdemySync bot engine (Poller or Telegram Listener) continuously in a background thread.
3. Exposes a live status dashboard at `/` and health check at `/health`.
4. Keeps the service active 24/7/365 (via internal self-ping and UptimeRobot compatibility).
"""

import os
import sys
import time
import asyncio
import logging
import threading
import urllib.request
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string

# ─────────────────────────────────────────────────────────────
# Setup and Configuration
# ─────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("RenderService")

app = Flask(__name__)
START_TIME = time.time()
BOT_STATUS = {
    "state": "starting",
    "mode": os.getenv("RUN_MODE", "poller").lower(),
    "last_error": None,
    "last_active": None,
    "restarts": 0,
    "self_ping_count": 0,
}

# Required environment check
def check_missing_config():
    missing = []
    if not os.getenv("UDEMY_ACCESS_TOKEN"):
        missing.append("UDEMY_ACCESS_TOKEN")

    has_bot_token = bool(os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"))
    has_api = bool(os.getenv("API_ID") and os.getenv("API_HASH"))
    has_session = bool(os.getenv("SESSION_STRING") or os.getenv("TELEGRAM_SESSION_STRING") or os.path.exists("scholarsync_session.session"))

    mode = os.getenv("RUN_MODE", "poller").lower()

    if mode == "listener":
        if not has_api:
            missing.append("API_ID, API_HASH")
        if not has_session:
            missing.append("SESSION_STRING")
        if not os.getenv("TARGET_CHANNELS"):
            missing.append("TARGET_CHANNELS")
    else:
        # Poller mode: only needs BOT_TOKEN (or API_ID/HASH/SESSION) to send alerts
        if not has_bot_token and not (has_api and has_session):
            missing.append("BOT_TOKEN (or API_ID + API_HASH + SESSION_STRING)")

    if not os.getenv("ALERT_CHANNEL_ID"):
        missing.append("ALERT_CHANNEL_ID")

    return missing


# ─────────────────────────────────────────────────────────────
# Background Bot Runner
# ─────────────────────────────────────────────────────────────
def run_bot_loop():
    """Runs the Telegram poller or listener in an isolated asyncio loop."""
    time.sleep(2)  # Allow web server to bind port first
    mode = os.getenv("RUN_MODE", "poller").lower()
    BOT_STATUS["mode"] = mode

    while True:
        missing = check_missing_config()
        if missing:
            BOT_STATUS["state"] = "awaiting_config"
            BOT_STATUS["last_error"] = f"Missing environment variables: {', '.join(missing)}"
            logger.warning("Bot waiting for configuration: %s", BOT_STATUS["last_error"])
            time.sleep(30)
            continue

        try:
            BOT_STATUS["state"] = "running"
            BOT_STATUS["last_error"] = None
            BOT_STATUS["last_active"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.info("Starting UdemySync bot in [%s] mode...", mode)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            if mode == "listener":
                import main
                loop.set_exception_handler(main._asyncio_exception_handler)
                loop.run_until_complete(main._main())
            else:
                import poller_main
                loop.run_until_complete(poller_main._main())

        except Exception as exc:
            BOT_STATUS["state"] = "error"
            BOT_STATUS["last_error"] = str(exc)
            BOT_STATUS["restarts"] += 1
            logger.error("Bot crashed with error: %s. Restarting in 15 seconds...", exc)
            time.sleep(15)


# ─────────────────────────────────────────────────────────────
# Background Keep-Alive / Self-Pinger
# ─────────────────────────────────────────────────────────────
def run_self_ping():
    """Periodically pings the web service to prevent Render Free tier from idling."""
    time.sleep(30)
    external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("APP_URL")

    while True:
        try:
            url = external_url if external_url else f"http://127.0.0.1:{os.getenv('PORT', 10000)}/health"
            req = urllib.request.Request(url, headers={"User-Agent": "UdemySync-KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    BOT_STATUS["self_ping_count"] += 1
                    BOT_STATUS["last_active"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception as exc:
            logger.debug("Self-ping: %s", exc)

        # Ping every 8 minutes (Render sleeps after 15 mins)
        time.sleep(480)


# Start background workers once on launch
bot_thread = threading.Thread(target=run_bot_loop, daemon=True, name="BotWorker")
bot_thread.start()

if os.getenv("SELF_PING", "true").lower() in ("true", "1", "yes"):
    ping_thread = threading.Thread(target=run_self_ping, daemon=True, name="SelfPinger")
    ping_thread.start()


# ─────────────────────────────────────────────────────────────
# Flask Web Routes
# ─────────────────────────────────────────────────────────────
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UdemySync — 24/7/365 Service Monitor</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #090d16;
            --card-bg: #111827;
            --border: #1f293d;
            --text: #f3f4f6;
            --text-muted: #9ca3af;
            --accent: #6366f1;
            --success: #10b981;
            --warning: #f59e0b;
            --error: #ef4444;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg);
            color: var(--text);
            font-family: 'Plus Jakarta Sans', sans-serif;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 24px;
        }
        .container {
            width: 100%;
            max-width: 780px;
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 16px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.5);
            overflow: hidden;
        }
        .header {
            padding: 28px 32px;
            background: linear-gradient(135deg, rgba(99,102,241,0.15), rgba(16,185,129,0.1));
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .title h1 {
            font-size: 24px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .title p {
            color: var(--text-muted);
            font-size: 14px;
            margin-top: 4px;
        }
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 14px;
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 600;
            font-family: 'JetBrains Mono', monospace;
        }
        .badge-running { background: rgba(16,185,129,0.2); color: var(--success); border: 1px solid var(--success); }
        .badge-starting { background: rgba(245,158,11,0.2); color: var(--warning); border: 1px solid var(--warning); }
        .badge-error { background: rgba(239,68,68,0.2); color: var(--error); border: 1px solid var(--error); }
        .body { padding: 32px; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .stat-card {
            background: rgba(255,255,255,0.02);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
        }
        .stat-label {
            font-size: 12px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .stat-value {
            font-size: 18px;
            font-weight: 600;
            font-family: 'JetBrains Mono', monospace;
            margin-top: 6px;
            color: #fff;
        }
        .alert-box {
            background: rgba(245, 158, 11, 0.1);
            border: 1px solid var(--warning);
            border-radius: 10px;
            padding: 16px;
            margin-bottom: 24px;
            font-size: 14px;
        }
        .alert-box.error {
            background: rgba(239, 68, 68, 0.1);
            border-color: var(--error);
        }
        .alert-title {
            font-weight: 600;
            margin-bottom: 4px;
        }
        .info-card {
            background: rgba(0,0,0,0.25);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 18px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            line-height: 1.6;
        }
        .footer {
            padding: 16px 32px;
            background: rgba(0,0,0,0.2);
            border-top: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
            color: var(--text-muted);
        }
        .footer a {
            color: var(--accent);
            text-decoration: none;
        }
        .pulse-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: currentColor;
            display: inline-block;
            box-shadow: 0 0 8px currentColor;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="title">
                <h1>🎓 UdemySync Service</h1>
                <p>Automatic Udemy coupon claimer & notification bot</p>
            </div>
            <div class="badge badge-{{ 'running' if status.state == 'running' else ('starting' if status.state == 'starting' or status.state == 'awaiting_config' else 'error') }}">
                <span class="pulse-dot"></span> {{ status.state.upper() }}
            </div>
        </div>
        <div class="body">
            {% if missing_keys %}
            <div class="alert-box">
                <div class="alert-title">⚙️ Setup Required in Render Dashboard</div>
                <div>Please configure the following Environment Variables under your Render Web Service settings:</div>
                <div style="margin-top: 8px; font-family: 'JetBrains Mono', monospace; font-weight: bold; color: var(--warning);">
                    {{ missing_keys | join(', ') }}
                </div>
            </div>
            {% endif %}

            {% if status.last_error and not missing_keys %}
            <div class="alert-box error">
                <div class="alert-title">⚠️ Latest Bot Log/Notice</div>
                <div style="font-family: 'JetBrains Mono', monospace; word-break: break-all;">{{ status.last_error }}</div>
            </div>
            {% endif %}

            <div class="grid">
                <div class="stat-card">
                    <div class="stat-label">Execution Mode</div>
                    <div class="stat-value">{{ status.mode.capitalize() }}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Service Uptime</div>
                    <div class="stat-value">{{ uptime }}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Keep-Alive Pings</div>
                    <div class="stat-value">{{ status.self_ping_count }} pings</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Restarts / Recoveries</div>
                    <div class="stat-value">{{ status.restarts }}</div>
                </div>
            </div>

            <div class="info-card">
                <div style="color: var(--text-muted); margin-bottom: 6px;">// 24/7/365 HEALTH & MONITORING</div>
                <div>✓ Web Service Port: <b>{{ port }}</b> (Bound & Healthy)</div>
                <div>✓ Keep-Alive URL: <b>/health</b> (HTTP 200 OK)</div>
                <div>✓ Last Active Ping: <b>{{ status.last_active or 'Just started' }}</b></div>
                <div>✓ Cloudflare TLS Spoofing: <b>curl_cffi Active</b></div>
            </div>
        </div>
        <div class="footer">
            <span>Powered by <b>Render Cloud</b> &bull; 24/7/365 Continuous</span>
            <a href="/health" target="_blank">View /health JSON &rarr;</a>
        </div>
    </div>
</body>
</html>
"""


@app.route("/")
def index():
    uptime_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    return render_template_string(
        DASHBOARD_HTML,
        status=BOT_STATUS,
        uptime=uptime_str,
        port=os.getenv("PORT", 10000),
        missing_keys=check_missing_config(),
    )


@app.route("/health")
@app.route("/ping")
def health():
    return jsonify({
        "status": "ok",
        "service": "UdemySync",
        "bot_state": BOT_STATUS["state"],
        "mode": BOT_STATUS["mode"],
        "uptime_seconds": int(time.time() - START_TIME),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }), 200


# ─────────────────────────────────────────────────────────────
# Local Direct Run
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
