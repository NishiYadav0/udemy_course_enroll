#!/bin/bash
# logs.sh — read ScholarSync's live logs without drowning in noise.
#
#   ./logs.sh            live feed, only the lines that matter  (Ctrl+C to stop)
#   ./logs.sh all        live feed, absolutely everything
#   ./logs.sh recent     last 120 meaningful lines, then exit
#   ./logs.sh enrolled   every successful enrollment so far
#   ./logs.sh dropped    every course that was dropped, with the reason
#   ./logs.sh errors     errors and warnings only
#   ./logs.sh share      compact block ready to paste into chat
#   ./logs.sh status     is the service alive? plus memory and uptime
#
# Reading logs never affects the bot — it only opens the file.

LOG="/home/ubuntu/scholarsync/scholarsync.log"

# Lines worth seeing: the pipeline decisions, not Pyrogram's chatter
KEEP='New post|Keyword|Button URLs|Resolving Udemy|Link [0-9]+/|Processing slug|Coupon check|Coupon codes in post|Policy result|Enroll result|ENROLLED|DROPPED|EXPIRED|Clicked enroll|checkout.?submit|reported success|confirmed checkout|Ownership confirmed|Express checkout completed|Retry|retry|No Udemy links|Found .* Udemy link|Scraper:|ERROR|WARNING|Token|token|listening for new posts|Heartbeat|Cloudflare|Alert sent|alert'

if [ ! -f "$LOG" ]; then
    echo "Log file not found: $LOG"
    echo "Is the service running?   sudo systemctl status scholarsync"
    exit 1
fi

case "$1" in
  all)
    echo "── Live feed (everything). Ctrl+C to stop ──"
    tail -f "$LOG"
    ;;
  recent)
    echo "── Last 120 meaningful lines ──"
    grep -E "$KEEP" "$LOG" | tail -n 120
    ;;
  enrolled)
    echo "── Successful enrollments ──"
    grep -E 'ENROLLED:|SUCCESS_ENROLLED' "$LOG" | grep -v DROPPED
    echo
    echo "Total: $(grep -cE 'ENROLLED:|SUCCESS_ENROLLED' "$LOG") line(s)"
    ;;
  dropped)
    echo "── Dropped courses and why ──"
    grep -E 'DROPPED' "$LOG" | tail -n 60
    echo
    echo "Most common reasons:"
    grep -oE 'DROPPED \| [^|]+' "$LOG" | sed 's/,.*//' | sort | uniq -c | sort -rn | head -8
    ;;
  errors)
    echo "── Errors and warnings ──"
    grep -E 'ERROR|WARNING|Traceback|Exception' "$LOG" | tail -n 60
    ;;
  share)
    echo "════════ ScholarSync log extract ════════"
    echo "Service : $(systemctl is-active scholarsync 2>/dev/null)"
    echo "Time    : $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "Enrolled so far: $(grep -cE 'ENROLLED:|SUCCESS_ENROLLED' "$LOG")"
    echo "─────────────────────────────────────────"
    grep -E "$KEEP" "$LOG" | tail -n 80
    echo "════════ end ════════"
    ;;
  status)
    systemctl status scholarsync --no-pager | head -n 14
    echo
    echo "── Memory (Chromium is heavy; watch for swap use) ──"
    free -h
    echo
    echo "── Last activity in the log ──"
    tail -n 3 "$LOG"
    ;;
  *)
    echo "── Live feed (filtered). Ctrl+C to stop ──"
    echo "   Waiting for the next Telegram post..."
    echo
    tail -f "$LOG" | grep --line-buffered -E "$KEEP"
    ;;
esac
