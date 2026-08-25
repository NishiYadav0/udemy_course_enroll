"""
admin_panel/env_editor.py
--------------------------
Reads and writes a .env file WITHOUT ever putting a full secret value in
front of the browser unless the admin explicitly asks to reveal it, and
without disturbing comments or ordering it doesn't touch.

Same backup-before-write pattern as apply_cookies.py (timestamped copy via
shutil.copy2 before any change), because that pattern has already proven
itself in this project.

Deliberately does NOT support adding brand-new keys from the web UI via
update_values(). Only keys that already exist in the file can be edited
through that path. This keeps the blast radius of a routine mistake small:
the day-to-day Environment page can update a stale token, it can't
accidentally create a malformed .env that breaks main.py's imports.

Two more functions live here for the one-time first-run setup wizard only
(never wired into the day-to-day Environment page): bootstrap_panel_env(),
which lets the panel boot before an admin account exists, and
append_or_update(), which — unlike update_values() — is allowed to create
keys that don't exist yet, because the wizard's whole job is writing a
.env that doesn't exist yet.
"""

import os
import re
import secrets
import shutil
from datetime import datetime
from typing import NamedTuple

KEY_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class EnvKey(NamedTuple):
    key: str
    masked_value: str
    has_value: bool


def mask(value: str) -> str:
    """Show only enough to confirm which credential this is — never the whole thing."""
    value = value.strip()
    if not value:
        return "(empty)"
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-3:]}  (len {len(value)})"


def list_keys(env_path: str) -> list[EnvKey]:
    """Return every KEY=value line in the file, masked, in file order."""
    if not os.path.exists(env_path):
        return []
    out: list[EnvKey] = []
    with open(env_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = KEY_LINE_RE.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2).strip()
            out.append(EnvKey(key=key, masked_value=mask(value), has_value=bool(value)))
    return out


def reveal_value(env_path: str, key: str) -> str | None:
    """Return the real value for ONE key. Only call this for an explicit admin reveal action."""
    if not os.path.exists(env_path):
        return None
    with open(env_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = KEY_LINE_RE.match(line)
            if m and m.group(1) == key:
                return m.group(2).strip()
    return None


def update_values(env_path: str, updates: dict[str, str], backup_dir: str | None = None) -> dict:
    """
    Replace the value of EXISTING keys only. Unknown keys are reported back,
    never appended. Every other line (comments, unrelated keys) is preserved
    exactly as-is.

    Returns {"replaced": [...], "unknown": [...], "backup_path": str|None}
    """
    if not os.path.exists(env_path):
        return {"replaced": [], "unknown": list(updates.keys()), "backup_path": None}

    backup_path = None
    if updates:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_name = f"{os.path.basename(env_path)}.backup-{stamp}"
        backup_path = os.path.join(backup_dir or os.path.dirname(env_path), backup_name)
        shutil.copy2(env_path, backup_path)

    with open(env_path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    remaining = dict(updates)
    replaced: list[str] = []
    out: list[str] = []

    for line in lines:
        m = KEY_LINE_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
            replaced.append(key)
        else:
            out.append(line)

    with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")

    return {"replaced": replaced, "unknown": list(remaining.keys()), "backup_path": backup_path}


def bootstrap_panel_env(env_path: str) -> None:
    """Create admin_panel/.env with nothing but a fresh SECRET_KEY, so the
    Flask app can boot (sessions, CSRF) on a completely fresh deploy —
    BEFORE an admin account exists. Called automatically by app.py's
    create_app() the moment it finds no SECRET_KEY at all.

    Never overwrites an existing file. ADMIN_USERNAME / ADMIN_PASSWORD_HASH
    are added moments later by the one-time /setup/claim route (or by
    running set_password.py over SSH, if you prefer that route instead) —
    setup_state.is_admin_claimed() checks for those two keys specifically,
    not just file existence, so this bootstrap-only state is correctly
    treated as "not claimed yet"."""
    if os.path.exists(env_path):
        return
    with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "# admin_panel/.env — the PANEL's OWN config. Different file from\n"
            "# the bot's .env one directory up. Never commit this file.\n"
            "# Auto-created on first boot, before an admin account exists.\n"
            "# Visit /setup/claim in a browser to finish setup (or run\n"
            "# set_password.py over SSH if you prefer that instead).\n"
            f"SECRET_KEY={secrets.token_hex(32)}\n"
            "SESSION_TIMEOUT_MINUTES=120\n"
            "BEHIND_HTTPS=true\n"
        )


def append_or_update(env_path: str, updates: dict[str, str], header_comment: str = "") -> dict:
    """Like update_values(), except a key that doesn't already exist is
    APPENDED rather than reported as unknown, and a completely missing file
    is created fresh. Same backup-first, preserve-everything-else behaviour
    for any file that did already exist.

    Used only by the one-time setup wizard (and set_password.py, its CLI
    equivalent) — deliberately never imported by the ordinary Environment
    page, which stays update-only by design (see module docstring).
    """
    backup_path = None
    if os.path.exists(env_path):
        if updates:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = f"{env_path}.backup-{stamp}"
            shutil.copy2(env_path, backup_path)
        with open(env_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    else:
        lines = []

    remaining = dict(updates)
    replaced: list[str] = []
    out: list[str] = []

    for line in lines:
        m = KEY_LINE_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
            replaced.append(key)
        else:
            out.append(line)

    appended: list[str] = []
    if remaining:
        if out and out[-1].strip():
            out.append("")
        if header_comment:
            out.append(f"# {header_comment}")
        for key, value in remaining.items():
            out.append(f"{key}={value}")
            appended.append(key)

    with open(env_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")

    return {"replaced": replaced, "appended": appended, "backup_path": backup_path}
