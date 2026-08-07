"""ScholarSync utility package."""

import threading

# ONE Chromium at a time, process-wide.
#
# This VM is an Oracle free-tier shape: 1 OCPU / 1 GB RAM. A single headless
# Chromium costs roughly 250-400 MB, so two at once already exceeds the box.
#
# main.py dispatches work through loop.run_in_executor(None, ...), whose
# default ThreadPoolExecutor holds (cpu_count + 4) threads. When several
# Telegram posts land within a minute — routine for these channels, which
# often mirror each other — each post independently launched a browser for the
# scraper's JS fallback. Observed live on 2026-08-07: four concurrent Chromium
# launches between 08:52:01 and 08:53:01, after which no scrape ever finished
# and Telegram's own connection was starved out with
# "ConnectionResetError" and "Retrying messages.GetDialogs - Request timed out".
#
# enroll_browser.py already had its own lock; the scraper did not, so the
# scraper alone could stampede. Both now share THIS lock, so scraping and
# enrolling can never run browsers simultaneously either.
#
# Serialising costs nothing real: coupons stay valid for days, and a queued
# post simply waits its turn.
BROWSER_LOCK = threading.Lock()
