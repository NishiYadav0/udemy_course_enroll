"""
admin_panel/log_viewer.py
---------------------------
Read-only access to scholarsync.log, for the web dashboard.

Reads only the TAIL of the file (last few MB), never the whole thing — the
log grows unbounded over weeks of operation, and this panel runs on a 1GB
VM alongside the bot itself, so loading a multi-hundred-MB file into memory
is not an option.

Category filters mirror logs.sh's own KEEP regex, so the web view and the
SSH view agree on what "an ENROLLED line" or "an ERROR line" means.
"""

import glob
import gzip
import os
import re
import time
from datetime import datetime

MAX_READ_BYTES = 4 * 1024 * 1024  # read at most the last 4MB of the log
ARCHIVE_MAX_AGE_DAYS = 14  # must match `maxage` in deploy/logrotate_scholarsync

# name -> (substring(s) to match, Bootstrap color class for highlighting)
CATEGORIES: dict[str, tuple[list[str], str]] = {
    "enrolled":  (["ENROLLED", "reported success", "confirmed checkout", "Ownership confirmed"], "success"),
    "dropped":   (["DROPPED", "EXPIRED"], "secondary"),
    "errors":    (["ERROR"], "danger"),
    "warnings":  (["WARNING"], "warning"),
    "retries":   (["Retry", "retry"], "info"),
    "cloudflare": (["Cloudflare"], "warning"),
    "posts":     (["New post", "listening for new posts"], "primary"),
}


def _classify(line: str) -> str | None:
    for name, (needles, _color) in CATEGORIES.items():
        if any(n in line for n in needles):
            return name
    return None


def category_color(name: str | None) -> str:
    if name and name in CATEGORIES:
        return CATEGORIES[name][1]
    return "light"


def tail_lines(log_path: str, max_lines: int = 500) -> list[str]:
    """Return up to the last `max_lines` lines of the log file."""
    if not os.path.exists(log_path):
        return []
    size = os.path.getsize(log_path)
    read_from = max(0, size - MAX_READ_BYTES)
    with open(log_path, "rb") as fh:
        fh.seek(read_from)
        chunk = fh.read()
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if read_from > 0 and lines:
        lines = lines[1:]  # first line may be a partial line — drop it
    return lines[-max_lines:]


def search(log_path: str, query: str = "", category: str = "", max_lines: int = 500) -> list[dict]:
    """
    Return matching lines (most recent last), each tagged with its category
    for colour-coding in the template.

    IMPORTANT LIMITATION, worth knowing: this searches within a recent
    window of the RAW file, not the file's entire history. A filter only
    finds matches inside that window — if the last time a channel ENROLLED
    something was further back than the window covers, filtering for
    "enrolled" won't surface it even though it's technically still in the
    (unrotated) file. The window scales with what you asked for (at least
    3x your requested line count, minimum 4000) precisely so that asking
    for "2000 lines" gives filtering real room to find them, but it is still
    capped at MAX_READ_BYTES (4MB) regardless — reading further back than
    that on every request would cost real CPU/RAM on a 1GB VM for a page
    that's meant to answer "what just happened," not archive browsing.
    Rotated (compressed) old logs from logrotate aren't browsable here at
    all yet — only via SSH (`zcat scholarsync.log.1.gz`).
    """
    window = max(4000, max_lines * 3)
    lines = tail_lines(log_path, max_lines=window)
    query_lower = query.strip().lower()

    results = []
    for line in lines:
        if category and _classify(line) != category:
            continue
        if query_lower and query_lower not in line.lower():
            continue
        results.append({"text": line, "category": _classify(line)})

    return results[-max_lines:]


def file_size_mb(path: str) -> float | None:
    """Current size of a file in MB, or None if it doesn't exist yet."""
    if not os.path.exists(path):
        return None
    return round(os.path.getsize(path) / (1024 * 1024), 2)


def list_archives(*live_log_paths: str) -> list[dict]:
    """
    Find rotated .gz archives sitting next to any of the given LIVE log
    paths — i.e. exactly what logrotate produces: scholarsync.log.1.gz,
    scholarsync.log.2.gz, etc.

    Deliberately strict about the filename pattern (basename + literal
    ".<digits>.gz") rather than just globbing "*.gz" in the directory —
    this same list is later used as the ONLY allowlist for which files the
    /archives/view route is allowed to open, so being loose here would
    widen what a request can read.
    """
    archives = []
    now = time.time()
    for live_path in live_log_paths:
        base = os.path.basename(live_path)
        pattern = re.compile(re.escape(base) + r"\.\d+\.gz$")
        for path in glob.glob(f"{live_path}.*.gz"):
            name = os.path.basename(path)
            if not pattern.match(name):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            age_days = (now - stat.st_mtime) / 86400
            archives.append({
                "name": name,
                "path": path,
                "source": base,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "age_days": round(age_days, 1),
                "expires_in_days": round(ARCHIVE_MAX_AGE_DAYS - age_days, 1),
            })
    archives.sort(key=lambda a: a["modified"], reverse=True)
    return archives


def read_archive(path: str, query: str = "", category: str = "", max_lines: int = 500) -> list[dict]:
    """
    Decompress and search ONE archive. Unlike the live log, this reads the
    whole file — archives are already capped at roughly the 25MB rotation
    threshold (see logrotate config), so even fully decompressed this is a
    bounded, occasional, user-initiated read, not something that happens on
    every page load like the live tail does.
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except (OSError, gzip.BadGzipFile):
        return []

    query_lower = query.strip().lower()
    results = []
    for line in lines:
        if category and _classify(line) != category:
            continue
        if query_lower and query_lower not in line.lower():
            continue
        results.append({"text": line, "category": _classify(line)})

    return results[-max_lines:]


def quick_stats(log_path: str) -> dict:
    """Last ENROLLED / DROPPED / ERROR line, for the dashboard summary cards."""
    lines = tail_lines(log_path, max_lines=4000)
    stats = {"last_enrolled": None, "last_dropped": None, "last_error": None, "total_scanned": len(lines)}
    for line in lines:
        cat = _classify(line)
        if cat == "enrolled":
            stats["last_enrolled"] = line
        elif cat == "dropped":
            stats["last_dropped"] = line
        elif cat == "errors":
            stats["last_error"] = line
    return stats
