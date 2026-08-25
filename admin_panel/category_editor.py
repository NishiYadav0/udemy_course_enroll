"""
admin_panel/category_editor.py
---------------------------------
Reads and writes config/category_policy.json — the admin-editable version
of what used to be two hardcoded dicts in utils/filter.py
(CATEGORY_MATRICES + DURATION_RULES). utils/filter.py loads this same file
at import time; if it's missing or broken, filter.py silently falls back
to its own built-in defaults, so nothing here can ever take the live bot's
category matching down (see that module's _load_policy()).

The FIRST time this page is opened on a given deploy (no JSON file yet),
load_policy() seeds one from filter.py's own built-in defaults — loaded
straight out of utils/filter.py via importlib rather than duplicated here
by hand, so the two can never drift apart. From then on, both the panel
(for editing) and the bot (for actual matching) read the same file.

Same backup-before-write pattern used throughout this project
(env_editor.py, apply_cookies.py).
"""

import importlib.util
import json
import os
import re
import shutil
from datetime import datetime

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
OTHER_NAME = "other"


def _policy_path(scholarsync_root: str) -> str:
    return os.path.join(scholarsync_root, "config", "category_policy.json")


def _bot_defaults(scholarsync_root: str) -> tuple[dict[str, list[str]], dict[str, float]]:
    """Load utils/filter.py's own _DEFAULT_* dicts straight from the source
    file on disk — never duplicated by hand here, so there's no way for
    this module's idea of "the defaults" to drift from the bot's."""
    filter_path = os.path.join(scholarsync_root, "utils", "filter.py")
    spec = importlib.util.spec_from_file_location("_bot_filter_defaults", filter_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._DEFAULT_CATEGORY_MATRICES, module._DEFAULT_DURATION_RULES


def _seed_from_defaults(scholarsync_root: str) -> dict:
    matrices, rules = _bot_defaults(scholarsync_root)
    categories = [
        {"name": name, "keywords": keywords, "min_hours": rules.get(name, 8.0)}
        for name, keywords in matrices.items()
    ]
    return {"categories": categories, "other_min_hours": rules.get(OTHER_NAME, 8.0)}


def load_policy(scholarsync_root: str) -> dict:
    """Returns {"categories": [{"name", "keywords": [...], "min_hours"}, ...],
    "other_min_hours": float}. Seeds the file from the bot's built-in
    defaults on first-ever call for a deploy, so the admin always sees real
    starting categories rather than a blank page."""
    path = _policy_path(scholarsync_root)
    if not os.path.exists(path):
        policy = _seed_from_defaults(scholarsync_root)
        _write(path, policy)
        return policy

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not data.get("categories"):
            raise ValueError("empty categories list")
        return data
    except Exception:
        # Broken file — reseed from defaults rather than showing an error
        # page. The bot side (utils/filter.py) has its own independent
        # fallback too, so a bad file was never able to break enrollment
        # even before this reseed happens.
        policy = _seed_from_defaults(scholarsync_root)
        _write(path, policy)
        return policy


def _write(path: str, policy: dict, backup: bool = True) -> str | None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup_path = None
    if backup and os.path.exists(path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{path}.backup-{stamp}"
        shutil.copy2(path, backup_path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(policy, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return backup_path


def _parse_keywords(raw: str) -> list[str]:
    # Accepts either comma-separated or one-per-line — whichever the admin
    # naturally pastes in.
    parts = re.split(r"[,\n]", raw)
    return [p.strip().lower() for p in parts if p.strip()]


def add_category(scholarsync_root: str, name: str, keywords_raw: str, min_hours: float) -> dict:
    """Returns {"ok": True} or {"ok": False, "error": ...}."""
    name = name.strip().lower().replace(" ", "_")
    if not NAME_RE.match(name):
        return {"ok": False, "error": "Category name must be lowercase letters, numbers, and underscores only, starting with a letter."}
    if name == OTHER_NAME:
        return {"ok": False, "error": "'other' is the built-in fallback category and already exists — edit its duration below instead."}

    policy = load_policy(scholarsync_root)
    if any(c["name"] == name for c in policy["categories"]):
        return {"ok": False, "error": f"A category named '{name}' already exists."}

    keywords = _parse_keywords(keywords_raw)
    if not keywords:
        return {"ok": False, "error": "Add at least one keyword — that's what a post is matched against."}

    policy["categories"].append({"name": name, "keywords": keywords, "min_hours": max(0.0, float(min_hours))})
    _write(_policy_path(scholarsync_root), policy)
    return {"ok": True}


def update_category(scholarsync_root: str, name: str, keywords_raw: str, min_hours: float) -> dict:
    policy = load_policy(scholarsync_root)
    cat = next((c for c in policy["categories"] if c["name"] == name), None)
    if not cat:
        return {"ok": False, "error": f"Category '{name}' not found."}

    keywords = _parse_keywords(keywords_raw)
    if not keywords:
        return {"ok": False, "error": "A category needs at least one keyword."}

    cat["keywords"] = keywords
    cat["min_hours"] = max(0.0, float(min_hours))
    _write(_policy_path(scholarsync_root), policy)
    return {"ok": True}


def delete_category(scholarsync_root: str, name: str) -> dict:
    if name == OTHER_NAME:
        return {"ok": False, "error": "'other' is the built-in fallback and can't be deleted."}
    policy = load_policy(scholarsync_root)
    before = len(policy["categories"])
    policy["categories"] = [c for c in policy["categories"] if c["name"] != name]
    if len(policy["categories"]) == before:
        return {"ok": False, "error": f"Category '{name}' not found."}
    _write(_policy_path(scholarsync_root), policy)
    return {"ok": True}


def update_other_min_hours(scholarsync_root: str, min_hours: float) -> dict:
    policy = load_policy(scholarsync_root)
    policy["other_min_hours"] = max(0.0, float(min_hours))
    _write(_policy_path(scholarsync_root), policy)
    return {"ok": True}
