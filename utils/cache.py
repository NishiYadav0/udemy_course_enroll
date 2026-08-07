"""
utils/cache.py
--------------
A small, thread-safe TTL cache with SINGLE-FLIGHT semantics.

Why this exists
---------------
Your three Telegram channels mirror each other. Observed live on 2026-08-07:

    08:52:01  ch=-1001154755160  freecourse.io/ai-engineer-professional...
    08:52:17  ch=-1001101378903  freecourse.io/ai-engineer-professional...

The SAME page, 16 seconds apart, from two different channels. Each one launched
its own headless Chromium (~20s, ~300 MB) to render an identical page. Across a
burst of 50-100 posts roughly 45% of all scraping is this kind of duplicate.

Two distinct problems, and this module solves both:

  1. REPEAT  — the same URL requested again a minute later.
               Solved by caching the result for `ttl` seconds.

  2. STAMPEDE — the same URL requested again while the first fetch is still
               running (exactly the 16-second case above). A plain cache does
               NOT help here, because nothing is cached yet — both callers miss
               and both launch a browser.
               Solved by a per-key lock: the second caller blocks until the
               first finishes, then reuses its result. This is "single-flight".

Design notes
------------
* Nothing here knows anything about Udemy, scraping or Telegram. It is a plain
  utility, so it cannot break pipeline logic.
* Failures are NOT cached. If a fetch returns nothing or raises, the next caller
  retries properly rather than inheriting a bad empty result for `ttl` seconds.
* Bounded: expired entries are swept on write, and `max_entries` caps growth so
  a long burst can never grow memory without limit.
"""

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class TTLCache:
    """Thread-safe TTL cache that also collapses concurrent duplicate work."""

    def __init__(self, ttl_seconds: float = 900, max_entries: int = 500, name: str = "cache"):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.name = name

        self._store: dict[Any, tuple[float, Any]] = {}   # key -> (expires_at, value)
        self._keylocks: dict[Any, threading.Lock] = {}   # key -> in-flight lock
        self._guard = threading.Lock()                   # protects both dicts

        self.hits = 0
        self.misses = 0
        self.collapsed = 0    # duplicate concurrent requests avoided

    # ── internals ────────────────────────────────────────────────────
    def _sweep_locked(self) -> None:
        """Drop expired entries. Caller must already hold self._guard."""
        now = time.monotonic()
        dead = [k for k, (exp, _) in self._store.items() if exp <= now]
        for k in dead:
            self._store.pop(k, None)

        # Hard cap as a safety net against unbounded growth
        if len(self._store) > self.max_entries:
            oldest = sorted(self._store.items(), key=lambda kv: kv[1][0])
            for k, _ in oldest[: len(self._store) - self.max_entries]:
                self._store.pop(k, None)

    def _peek(self, key: Any) -> tuple[bool, Any]:
        """Return (found, value) without computing anything."""
        with self._guard:
            entry = self._store.get(key)
            if entry and entry[0] > time.monotonic():
                return True, entry[1]
        return False, None

    def _key_lock(self, key: Any) -> threading.Lock:
        with self._guard:
            lock = self._keylocks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._keylocks[key] = lock
            return lock

    # ── public API ───────────────────────────────────────────────────
    def get_or_compute(
        self,
        key: Any,
        producer: Callable[[], Any],
        is_valid: Callable[[Any], bool] | None = None,
        label: str = "",
    ) -> Any:
        """
        Return the cached value for `key`, or compute it exactly once.

        producer  : zero-arg callable that does the expensive work
        is_valid  : optional check — only results passing it are cached.
                    Use this so empty/failed results are never memoised.
        label     : human-readable name for the log line
        """
        shown = label or str(key)[:70]

        found, value = self._peek(key)
        if found:
            self.hits += 1
            logger.info("Cache hit (%s) — skipping work for %s", self.name, shown)
            return value

        lock = self._key_lock(key)
        already_running = not lock.acquire(blocking=False)

        if already_running:
            # Someone else is fetching this exact key right now. Wait for them
            # instead of duplicating a 20-second browser launch.
            self.collapsed += 1
            logger.info("Duplicate request (%s) — waiting for in-flight fetch of %s",
                        self.name, shown)
            lock.acquire()

        try:
            # Re-check: the holder we just waited on has probably filled it in.
            found, value = self._peek(key)
            if found:
                self.hits += 1
                if already_running:
                    logger.info("Reused in-flight result (%s) for %s", self.name, shown)
                return value

            self.misses += 1
            result = producer()

            ok = is_valid(result) if is_valid else (result is not None)
            if ok:
                with self._guard:
                    self._store[key] = (time.monotonic() + self.ttl, result)
                    self._sweep_locked()
            else:
                logger.debug("Not caching empty/failed result (%s) for %s", self.name, shown)
            return result
        finally:
            lock.release()

    def peek(self, key: Any) -> tuple[bool, Any]:
        """
        Look up `key` without computing anything.

        Returns (found, value). Use this for simple "have I already handled
        this?" checks, where there is no expensive producer to run — the
        caller just wants to skip.
        """
        return self._peek(key)

    def remember(self, key: Any, value: Any) -> None:
        """
        Store a value directly, for callers that computed it themselves.

        Pairs with peek() for the skip-if-seen pattern:

            found, prev = CACHE.peek(k)
            if found:
                return                    # already handled
            result = do_work()
            if is_terminal(result):
                CACHE.remember(k, result)

        Storing only terminal results keeps transient failures retryable.
        """
        with self._guard:
            self._store[key] = (time.monotonic() + self.ttl, value)
            self._sweep_locked()

    def invalidate(self, key: Any) -> None:
        with self._guard:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._guard:
            self._store.clear()

    def stats(self) -> str:
        total = self.hits + self.misses
        rate = (self.hits / total * 100) if total else 0.0
        return (f"{self.name}: {self.hits} hit / {self.misses} miss "
                f"({rate:.0f}% saved), {self.collapsed} duplicate(s) collapsed, "
                f"{len(self._store)} cached")


class PersistentTTLCache(TTLCache):
    """
    A TTLCache that survives restarts by mirroring itself to a JSON file.

    Used for course OWNERSHIP, which is the one fact here that genuinely does
    not change: you cannot un-own a Udemy course. Keeping it only in memory
    meant every restart — and there are many during development — threw the
    knowledge away, so the next repost of an already-owned course cost a full
    ~20s browser scrape plus API calls just to rediscover it.

    Safety properties, in order of importance:

    * WRITTEN ONLY ON CONFIRMATION. The caller stores an entry only after Udemy
      itself said so (a completed enrollment, or is_valid_student=true). We
      never persist a guess.
    * SELF-HEALING. Entries carry an absolute expiry (default 30 days). Even if
      a bad entry somehow got written, it disappears on its own rather than
      blocking that course forever.
    * FAILS OPEN. A missing, unreadable or corrupt file logs a warning and
      starts empty. The bot then simply re-learns ownership the slow-but-correct
      way. A cache problem can never stop the pipeline.
    * ATOMIC WRITES. Saved to a temp file then renamed, so a crash mid-write
      cannot leave a half-written file behind.

    Note on clocks: TTLCache uses time.monotonic() (immune to clock changes),
    but monotonic resets on reboot, so the FILE stores wall-clock expiry and we
    convert back on load.
    """

    def __init__(self, path: str, ttl_seconds: float, max_entries: int = 5000,
                 name: str = "persistent"):
        super().__init__(ttl_seconds=ttl_seconds, max_entries=max_entries, name=name)
        self.path = path
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            logger.info("%s: no saved file yet (%s) — starting empty", self.name, self.path)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:
            logger.warning("%s: could not read %s (%s) — starting empty",
                           self.name, self.path, exc)
            return

        now_wall = time.time()
        now_mono = time.monotonic()
        loaded = expired = 0
        for key, entry in (raw or {}).items():
            try:
                expires_at = float(entry["expires_at"])
                value = entry.get("value")
            except Exception:
                continue
            remaining = expires_at - now_wall
            if remaining <= 0:
                expired += 1
                continue
            self._store[key] = (now_mono + remaining, value)
            loaded += 1

        logger.info("%s: restored %d course(s) from %s (%d expired)",
                    self.name, loaded, os.path.basename(self.path), expired)

    def _save(self) -> None:
        """Atomically write the current contents with wall-clock expiries."""
        now_wall = time.time()
        now_mono = time.monotonic()
        with self._guard:
            snapshot = {
                key: {"value": value, "expires_at": now_wall + (exp - now_mono)}
                for key, (exp, value) in self._store.items()
                if exp > now_mono
            }
        try:
            directory = os.path.dirname(self.path) or "."
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cache-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            os.replace(tmp, self.path)          # atomic on POSIX
        except Exception as exc:
            # Never let a disk problem break enrollment.
            logger.warning("%s: could not save %s — %s", self.name, self.path, exc)

    def remember(self, key: Any, value: Any) -> None:
        super().remember(key, value)
        self._save()


# ─────────────────────────────────────────────────────────────
# Shared instances
# ─────────────────────────────────────────────────────────────

# Intermediary coupon pages (freecourse.io, findmycourse.in ...).
# 15 minutes: long enough to absorb a mirrored burst across all three channels,
# short enough that a genuinely updated page is picked up soon after. The retry
# queue deliberately BYPASSES this cache, because its whole purpose is to notice
# coupons that were added to a page after we first read it.
SCRAPE_CACHE = TTLCache(ttl_seconds=900, max_entries=300, name="scrape")

# Udemy course links already put through the pipeline recently. Stops the same
# course being re-processed once per mirroring channel.
# Keyed on slug::coupon — a DIFFERENT coupon for the same course is still
# allowed through, because if the first coupon was dead the second may work.
COURSE_CACHE = TTLCache(ttl_seconds=600, max_entries=500, name="course")

# Courses we have CONFIRMED we own — either just enrolled, or Udemy's
# is_valid_student said we already had it.
#
# Keyed on the SLUG ONLY, deliberately. Once a course is owned, every coupon
# for it is pointless: a second code cannot enrol you twice. COURSE_CACHE's
# slug::coupon key would let coupon B through after coupon A succeeded, which
# costs a full metadata + ownership round-trip to learn nothing.
#
# PERSISTED to disk, because ownership genuinely never changes and this is the
# one piece of state worth surviving a restart. Without persistence, every
# restart threw it away and the next repost of an already-owned course cost a
# full browser scrape to rediscover something unchangeable.
#
# 30 days rather than forever: long enough that reposts are always free, short
# enough that any entry written in error heals itself instead of blocking that
# course permanently.
#
# This also enables PRE-SCRAPE skipping: the coupon sites use the same slug in
# their own URLs (verified 4/4 against live data), e.g.
#     https://freecourse.io/courses/lpi-linux-essentials-010-160-exam-questions
#     https://www.udemy.com/course/lpi-linux-essentials-010-160-exam-questions/
# so a mirrored post can be recognised and dropped BEFORE launching a browser.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNED_STORE_PATH = os.path.join(_PROJECT_ROOT, "enrolled_courses.json")

ENROLLED_SLUGS = PersistentTTLCache(
    path=OWNED_STORE_PATH,
    ttl_seconds=30 * 24 * 3600,     # 30 days
    max_entries=5000,
    name="owned",
)
