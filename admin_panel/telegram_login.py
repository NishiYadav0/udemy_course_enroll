"""
admin_panel/telegram_login.py
-------------------------------
Runs the ONE-TIME Telegram "userbot" login handshake (phone number -> code
-> optional 2FA password) from inside the web setup wizard, so a new admin
never has to SSH in and answer main.py's interactive input() prompts by
hand — this is genuinely the trickiest part of "just deploy and log in".

Kurigram/Pyrogram's login calls are async; Flask, run via Gunicorn's sync
worker, is not. A fresh event loop per HTTP request won't work here because
the SAME live connection has to survive from "send the code" (request 1) to
"verify the code" (request 2, maybe a minute later) — asyncio transports
are bound to the loop that created them. So instead: one small background
thread owns a single long-lived event loop for the whole life of the panel
process, and Flask routes hand it work with
asyncio.run_coroutine_threadsafe(...), blocking briefly for the result.

Login attempts are short-lived, in-memory state only (dict keyed by a
random token, same pattern as wizard_state.py) — nothing here touches disk
except the real Pyrogram .session file, written by Pyrogram itself once
login actually succeeds. An abandoned attempt is swept out after 10 minutes
so a half-finished login can never pin a Telegram connection open forever.

Also provides resolve_channel(), used on the Channels wizard step: once the
session is authorized, it lets an admin type "@channelname" instead of
having to go dig up a numeric chat ID by hand — main.py's TARGET_CHANNELS /
ALERT_CHANNEL_ID both require plain integers (see main.py's _require_env),
so resolving the human-friendly name to that integer here is what makes the
rest of the wizard fully non-technical.
"""

import asyncio
import secrets
import threading
import time

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    RPCError,
    SessionPasswordNeeded,
)

SESSION_NAME = "scholarsync_session"  # MUST match main.py's Client(name=...) exactly
_SESSION_TTL_SECONDS = 10 * 60

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()

_attempts: dict[str, dict] = {}
_attempts_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop_thread is None or not _loop_thread.is_alive():
            _loop = asyncio.new_event_loop()

            def _run(loop: asyncio.AbstractEventLoop) -> None:
                asyncio.set_event_loop(loop)
                loop.run_forever()

            _loop_thread = threading.Thread(target=_run, args=(_loop,), daemon=True,
                                             name="telegram-login-loop")
            _loop_thread.start()
        return _loop


def _call(coro, timeout: float = 30.0):
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _sweep_expired() -> None:
    now = time.time()
    with _attempts_lock:
        expired = [tok for tok, a in _attempts.items() if now - a["created"] > _SESSION_TTL_SECONDS]
    for tok in expired:
        _drop(tok)


def _drop(token: str) -> None:
    with _attempts_lock:
        attempt = _attempts.pop(token, None)
    if attempt and attempt.get("client"):
        try:
            _call(attempt["client"].disconnect(), timeout=10.0)
        except Exception:
            pass


def start_login(api_id: int, api_hash: str, phone_number: str, workdir: str) -> dict:
    """Begin a login attempt: connect and ask Telegram to send the code.
    Returns {"ok": True, "token": ...} or {"ok": False, "error": ...}."""
    _sweep_expired()

    client = Client(name=SESSION_NAME, api_id=api_id, api_hash=api_hash, workdir=workdir)
    try:
        _call(client.connect())
        sent = _call(client.send_code(phone_number))
    except FloodWait as exc:
        return {"ok": False, "error": f"Telegram asked us to wait {exc.value}s before trying again."}
    except PhoneNumberInvalid:
        return {"ok": False, "error": "That phone number looks invalid — use international format, e.g. +91XXXXXXXXXX."}
    except RPCError as exc:
        return {"ok": False, "error": f"Telegram rejected this: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't reach Telegram: {exc}"}

    token = secrets.token_urlsafe(24)
    with _attempts_lock:
        _attempts[token] = {
            "client": client,
            "phone_number": phone_number,
            "phone_code_hash": sent.phone_code_hash,
            "created": time.time(),
        }
    return {"ok": True, "token": token}


def submit_code(token: str, code: str) -> dict:
    with _attempts_lock:
        attempt = _attempts.get(token)
    if not attempt:
        return {"ok": False, "error": "This login attempt expired or was never started. Start again."}

    try:
        _call(attempt["client"].sign_in(
            phone_number=attempt["phone_number"],
            phone_code_hash=attempt["phone_code_hash"],
            phone_code=code.strip(),
        ))
    except SessionPasswordNeeded:
        return {"ok": True, "needs_password": True}
    except (PhoneCodeInvalid, PhoneCodeExpired):
        return {"ok": False, "error": "That code was wrong or has expired — double check it, or start over for a new one."}
    except RPCError as exc:
        return {"ok": False, "error": f"Telegram rejected this: {exc}"}

    return _finish(token)


def submit_password(token: str, password: str) -> dict:
    with _attempts_lock:
        attempt = _attempts.get(token)
    if not attempt:
        return {"ok": False, "error": "This login attempt expired or was never started. Start again."}

    try:
        _call(attempt["client"].check_password(password))
    except PasswordHashInvalid:
        return {"ok": False, "error": "That 2FA password was wrong — try again."}
    except RPCError as exc:
        return {"ok": False, "error": f"Telegram rejected this: {exc}"}

    return _finish(token)


def _finish(token: str) -> dict:
    with _attempts_lock:
        attempt = _attempts.pop(token, None)
    if not attempt:
        return {"ok": False, "error": "Login session vanished — try again."}
    try:
        me = _call(attempt["client"].get_me())
    finally:
        try:
            _call(attempt["client"].disconnect(), timeout=10.0)
        except Exception:
            pass
    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or (me.username or str(me.id))
    return {"ok": True, "done": True, "name": name, "username": me.username}


def cancel(token: str) -> None:
    _drop(token)


def resolve_channel(api_id: int, api_hash: str, workdir: str, identifier: str) -> dict:
    """Turn a human-friendly '@channelname' (or a t.me link, or an already-
    numeric ID) into the exact numeric chat ID main.py's TARGET_CHANNELS /
    ALERT_CHANNEL_ID require. Only works AFTER Telegram login succeeded —
    it reconnects using the now-authorized session file, no fresh login
    needed. Returns {"ok": True, "id": ..., "title": ...} or an error."""
    identifier = identifier.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if identifier.startswith(prefix):
            identifier = "@" + identifier[len(prefix):].lstrip("/")
            break

    try:
        target: int | str = int(identifier)
    except ValueError:
        target = identifier if identifier.startswith("@") else f"@{identifier}"

    client = Client(name=SESSION_NAME, api_id=api_id, api_hash=api_hash, workdir=workdir)
    try:
        _call(client.connect())
        chat = _call(client.get_chat(target))
        title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat.id)
        return {"ok": True, "id": chat.id, "title": title}
    except RPCError as exc:
        return {"ok": False, "error": f"Couldn't find that channel: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"Couldn't resolve that: {exc}"}
    finally:
        try:
            _call(client.disconnect(), timeout=10.0)
        except Exception:
            pass
