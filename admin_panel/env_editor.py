"""
admin_panel/env_editor.py
--------------------------
Reads and writes a .env file WITHOUT ever putting a full secret value in
front of the browser unless the admin explicitly asks to reveal it, and
without disturbing comments or ordering it doesn't touch.

Same backup-before-write pattern as apply_cookies.py (timestamped copy via
shutil.copy2 before any change), because that pattern has already proven
itself in this project.

Deliberately does NOT support adding brand-new keys from the web UI. Only
keys that already exist in the file can be edited. This keeps the blast
radius of a mistake small: the panel can update a stale token, it can't
accidentally create a malformed .env that breaks main.py's imports.
"""

import os
import re
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
