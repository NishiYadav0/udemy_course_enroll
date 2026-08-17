# 🎓 ScholarSync

**Automatic Udemy coupon discovery, filtering, and enrollment — powered by Telegram.**

ScholarSync watches Telegram channels that post free Udemy coupon deals, extracts the real enrollment link (which is usually hidden behind an intermediary "coupon aggregator" website), checks the course against a configurable policy (category, duration, rating, language), and — if it passes — automatically claims it on your Udemy account and sends you a confirmation. All day, every day, with no manual checking.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey)
![Status](https://img.shields.io/badge/status-active-success)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Table of Contents

- [The Problem](#the-problem)
- [What ScholarSync Does](#what-scholarsync-does)
- [How It Works](#how-it-works)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Enrollment Policy](#enrollment-policy)
- [Handling Bursts and Duplicate Posts](#handling-bursts-and-duplicate-posts)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Local Setup](#local-setup)
- [Deploying to Production (Any Linux VPS)](#deploying-to-production-any-linux-vps)
  - [1. Connect via SSH / PuTTY](#1-connect-via-ssh--putty)
  - [2. Install Python 3.10+](#2-install-python-310)
  - [3. Transfer the Project (WinSCP / SCP)](#3-transfer-the-project-winscp--scp)
  - [4. Install Dependencies](#4-install-dependencies)
  - [5. First Run & Telegram Login](#5-first-run--telegram-login)
  - [6. Run It Forever with systemd](#6-run-it-forever-with-systemd)
  - [7. Install Xvfb and the browser](#7-install-xvfb-and-the-browser)
  - [8. Supply your Udemy session cookies](#8-supply-your-udemy-session-cookies)
- [Admin Panel](#admin-panel)
- [Testing & Verification](#testing--verification)
- [Troubleshooting & Lessons Learned](#troubleshooting--lessons-learned)
- [What Gets Pushed to GitHub](#what-gets-pushed-to-github)
- [Roadmap](#roadmap)
- [Security Notes](#security-notes)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## The Problem

Telegram has dozens of channels that post 100%-off Udemy coupons daily — but there are three catches:

1. The real Udemy link is almost never in the post itself. It's hidden behind a redirect on a third-party "coupon aggregator" site, so you have to manually click through every single post to find out if a course is even worth taking.
2. Coupons expire fast — often within hours, sometimes minutes — so by the time you check a post, the discount may already be gone.
3. Most posted courses are irrelevant filler (marketing courses, unrelated hobby content) buried among the handful that actually matter for a specific goal — in this project's case, courses relevant to an IIT Madras BS Data Science curriculum.

Manually monitoring multiple channels all day, clicking through every link, and judging every course on the spot isn't realistic. ScholarSync automates the entire chain: **detect → extract → evaluate → enroll → notify.**

## What ScholarSync Does

- 📡 **Listens live** to any number of Telegram channels (as your own account, via a "userbot") — no bot API, no channel admin permissions needed, just being a member.
- 🔗 **Extracts the real Udemy link** from post text, inline buttons ("Enroll Now"), or by visiting the linked intermediary page and scraping it — including JavaScript-rendered pages via a headless browser fallback.
- 🧠 **Categorizes** each course by keyword matching (Data Science, Coding, Ethical Hacking, Design, Marketing, Linguistics, or a catch-all "Other" bucket).
- ✅ **Applies a policy engine**: language filter, must be a genuinely paid course with a working (non-expired) coupon, minimum star rating, and a category-specific minimum duration — or an automatic pass for "bestseller"/"hot & new" badged courses.
- 🎯 **Auto-enrolls** via the Udemy API using your saved session token — no browser automation, no clicking through checkout.
- 🔔 **Sends you a confirmation alert** on your own private Telegram channel the moment a course is claimed.
- 🔁 **Retries expired coupons**: some source sites update the *same* course page with a fresh coupon code hours after the original post — a background worker rechecks longer courses (> 5h) periodically to catch this instead of giving up after one look.
- 🩺 **Self-healing deployment**: runs as a proper background service (systemd) that restarts itself automatically on crash or server reboot — no manual babysitting required.

## How It Works

```
        ┌─────────────────────────────────────────────────────────┐
        │  A new coupon post lands in one of your target channels  │
        └───────────────────────────┬─────────────────────────────┘
                                    ▼
   ┌────────────────────┐   main.py — Kurigram receives the live update
   │ 1  LISTEN          │   (no polling; Telegram pushes it to us)
   └────────┬───────────┘
            ▼
   ┌────────────────────┐   utils/filter.py — keyword_match()
   │ 2  CATEGORISE      │   → coding | data_science | design | … | other
   └────────┬───────────┘
            ▼
   ┌────────────────────┐   utils/scraper.py
   │ 3  RESOLVE LINK    │   Follow the intermediary site and extract every
   └────────┬───────────┘   real Udemy coupon URL (headless fallback for JS)
            ▼
   ┌────────────────────┐   utils/udemy.py — get_course_metadata()
   │ 4  METADATA        │   id · title · duration · rating · language
   └────────┬───────────┘
            ▼
   ┌────────────────────┐   utils/udemy.py — get_coupon_pricing()
   │ 5  COUPON CHECK    │   The REAL post-coupon price, coupon validity,
   └────────┬───────────┘   and whether you already own the course
            │
            ├─ already owned ─────────────────► skip quietly, no alert
            ├─ coupon dead / not $0 ──────────► EXPIRED → retry queue
            ▼
   ┌────────────────────┐   utils/filter.py — evaluate_course_policy()
   │ 6  POLICY          │   language · paid · rating · duration per category
   └────────┬───────────┘
            │
            ├─ fails a rule ──────────────────► DROPPED (reason logged)
            ▼
   ┌────────────────────┐   utils/udemy.py — auto_enroll()
   │ 7  ENROLL (API)    │   POST /users/me/subscribed-courses/ → 403
   └────────┬───────────┘   Expected: Udemy blocks non-browser clients
            ▼
   ┌────────────────────┐   utils/enroll_browser.py — browser_enroll()
   │ 8  ENROLL (BROWSER)│   Real Chrome under Xvfb with a persistent profile,
   └────────┬───────────┘   opens express checkout, clicks "Enroll now"
            ▼
   ┌────────────────────┐   Udemy replies {"status":"succeeded"}
   │ 9  CONFIRM         │   Double-checked against the ownership API
   └────────┬───────────┘
            ▼
   ┌────────────────────┐   utils/notifier.py
   │ 10 NOTIFY          │   Success alert → your private Telegram channel
   └────────────────────┘

   Running alongside, forever:
     • heartbeat      every 10 min — proves the listener is still alive
     • retry worker   every 15 min — re-checks queued courses (only those
                                     over 5 hrs; given up on after 3 hrs)
```

**Roughly 5 seconds** from post to decision, and **about 55 seconds** for a full
browser enrollment. Enrollments are serialised — one browser at a time — so a
burst of posts is processed back to back rather than in parallel.

## Tech Stack

| Library | Role |
|---|---|
| [Kurigram](https://github.com/KurimuzonAkuma/pyrogram) | Telegram MTProto client (userbot) — a maintained fork of the original Pyrogram, needed because the original stopped receiving updates for newer Telegram protocol layers |
| [Playwright](https://playwright.dev/python/) | Drives a real Chrome for both JS-rendered scraping **and** the actual enrollment |
| [playwright-stealth](https://pypi.org/project/playwright-stealth/) | Hides automation fingerprints so Cloudflare doesn't challenge the checkout page |
| [curl_cffi](https://github.com/lexiforest/curl_cffi) | Impersonates Chrome's TLS handshake for API calls that would otherwise be flagged |
| [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) | HTML parsing for link extraction |
| [Requests](https://requests.readthedocs.io/) | Fast static fetching, tried before falling back to a full browser |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Loads secrets from a local `.env` file |
| **Xvfb** (system package) | Virtual display so Chrome runs as a real windowed browser on a headless server |

**Admin panel only** (separate `admin_panel/requirements.txt`, not needed to run the bot itself):

| Library | Role |
|---|---|
| [Flask](https://flask.palletsprojects.com/) | Server-rendered web app framework |
| [Flask-Login](https://flask-login.readthedocs.io/) | Session-based authentication |
| [Flask-WTF](https://flask-wtf.readthedocs.io/) | CSRF protection on every form |
| [Gunicorn](https://gunicorn.org/) | Production WSGI server, run behind nginx |
| **Nginx** (system package) | Reverse proxy, HTTPS termination, IP allowlisting |
| [Bootstrap 5](https://getbootstrap.com/) (CDN) | UI styling — no build step, no npm |

Python **3.10+** is required (the codebase uses modern type-hint syntax like `str | None`).

## Project Structure

```
ScholarSync/
│
├── main.py                     ★ THE BOT — entry point, run this
├── requirements.txt            ★ Python dependencies
├── .env                        ✗ your secrets (git-ignored — NEVER commit)
├── config/.env.example         ★ template showing what .env needs
│
├── utils/                      ★ core package
│   ├── filter.py               ·  category keywords + enrollment policy
│   ├── scraper.py              ·  intermediary-site → real Udemy links
│   ├── udemy.py                ·  metadata, coupon pricing, ownership, enroll
│   ├── enroll_browser.py       ·  browser-based enrollment (the part that works)
│   ├── cache.py                ·  TTL + single-flight caches (burst de-duplication)
│   ├── notifier.py             ·  alert message formatting
│   └── retry_queue.py          ·  re-checks courses whose coupon had died
│
├── apply_cookies.py            ★ helper: writes Udemy cookies into .env
├── test_scrape.py              ★ dry run: what the scraper gets + policy verdict
├── test_enrollment.py          ★ test one course end to end (bypasses policy)
├── logs.sh                     ★ read the live logs without the noise
├── test_pipeline.py            ○ replays a real post through the whole pipeline
├── debug_listener.py           ○ proves live Telegram updates are arriving
├── diagnose.py                 ○ one-pass check: channels, scraper, token
│
├── poller_main.py              ○ abandoned architecture: poll websites instead
├── utils/poller.py             ○   of Telegram. Kept for reference only.
│
└── admin_panel/                ○ optional web dashboard — see Admin Panel section
    ├── app.py                  ·  Flask routes (dashboard, logs, archives, env editor, auth)
    ├── env_editor.py           ·  masked, format-preserving .env read/write
    ├── service_control.py      ·  systemctl wrappers (narrow sudo only)
    ├── log_viewer.py           ·  live-log search + .gz archive listing/reading
    ├── auth.py / audit.py      ·  login throttling + change history
    ├── set_password.py         ★ first-time setup / password reset
    ├── requirements.txt        ★ panel's own dependencies (separate from the bot's)
    ├── .env                    ✗ panel's own secrets (git-ignored — NEVER commit)
    ├── audit.log               ✗ change history (git-ignored, no secret values in it)
    ├── templates/, static/     ·  Bootstrap 5 UI (dashboard, logs, archives, env editor,
    │                              commands, audit, profile, login)
    └── deploy/                 ·  systemd units (panel + logrotate timer), nginx config,
                                    sudoers rule, logrotate policy, setup script

★ required   · part of the package   ○ optional / diagnostic   ✗ never commit
```

**Auto-generated at runtime** (all git-ignored): `scholarsync_session.session`,
`scholarsync.log`, `retry_queue.json`, `seen_courses.json`, `enroll_*.png`,
`enroll_failed.html`, `enrolled_courses.json`, `~/.scholarsync_browser/`.

## Enrollment Policy

A course is only enrolled if it clears **every** rule below, checked in this order:

1. **Language** — must be English or Hindi.
2. **Paid course** — natively free courses are skipped (this tool targets coupon-gated paid courses).
3. **Working coupon** — the price *with the coupon actually applied* must be $0. This is checked against Udemy's real pricing endpoint, not the list price and not the claim in the post.
4. **Rating** — must be ≥ 4.0★ **if the course has been rated at all**. Brand-new courses report `0.0`, which means *unrated*, not *bad* — and since free coupons are overwhelmingly used to launch new courses, unrated ones are allowed through (`ALLOW_UNRATED`).
5. **Popularity override** — a "bestseller"/"hot & new"/"highest rated" badge enrolls immediately, skipping the duration check.
6. **Minimum duration** (if not already popular):

| Category | Minimum duration |
|---|---|
| Data Science | > 3 hours |
| Coding | > 3 hours |
| Ethical Hacking | > 3 hours |
| Digital Marketing | > 6 hours |
| Design | > 6 hours |
| Linguistics | > 10 hours |
| Other (uncategorized) | > 8 hours |

**Practice-test courses are exempt from rule 6.** They contain no video, so Udemy
reports `0.0` hours and the duration rule could never be satisfied — every one of
them would be dropped. `ALLOW_ZERO_DURATION_PRACTICE_TESTS` handles this.

All of this lives in one file — `utils/filter.py` — so tuning the rules to your own
goals is a single-file edit. Both exemptions above are boolean toggles you can flip
back to strict behaviour.

## Handling Bursts and Duplicate Posts

Coupon channels do not deliver a tidy stream. They arrive in bursts, and because
most channels mirror the same sources, **the same course commonly arrives two or
three times within a minute**. Three concerns follow from that, and each is
handled by a different mechanism.

### 1. What happens when 100 posts arrive at once?

Nothing is lost. Telegram hands every post to the handler, and each one queues on
a **single worker thread**.

That single worker is deliberate. Both coupon sites are JavaScript-rendered, so
every scrape needs a real headless Chromium at roughly 300 MB. On a 1 GB VM the
default thread pool — `cpu_count + 4` — would happily start five at once and
exhaust the machine. When that happened in testing, *no* scrape completed and
Telegram's own connection was starved until it dropped.

```python
PIPELINE_EXECUTOR = ThreadPoolExecutor(max_workers=1)   # override: SCHOLARSYNC_WORKERS
```

Serialising costs nothing real: coupons stay valid for days, so a post waiting a
minute for its turn loses nothing.

### 2. The same course arriving from every channel

Three layers of de-duplication, applied cheapest-first:

| Layer | Runs | Keyed on | Prevents |
|---|---|---|---|
| **Pre-scrape skip** | Before any browser | course slug | Scraping a course you already own |
| **Scrape cache** | During scraping | page URL | 3 browsers for 1 page (15 min) |
| **Course cache** | After scraping | `slug::coupon` | Re-deciding the same offer (10 min) |

The pre-scrape skip works because both coupon sites reuse Udemy's own slug in
their URLs:

```
https://freecourse.io/courses/lpi-linux-essentials-010-160-exam-questions
https://www.udemy.com/course/lpi-linux-essentials-010-160-exam-questions/
                             └────────────── same slug ──────────────┘
```

So a post can be identified — and dropped — **before** a browser starts.

The scrape cache also collapses *concurrent* duplicates. A plain cache does not
help when two channels post the same URL 16 seconds apart, because nothing is
cached yet and both callers miss. A per-key lock makes the second caller wait for
the first and reuse its result ("single-flight").

Measured on a realistic burst — 3 channels × 10 posts, 6 shared:

| | Browser launches | Wall clock |
|---|---|---|
| Without de-duplication | 30 | 19.0 min |
| With de-duplication | 18 | **10.7 min** |

### 3. "Will it ever skip a course it should have taken?"

No — and this is the property the design is built around.

The two caches use **different keys**, and that distinction is what keeps every
genuine opportunity alive:

- `COURSE_CACHE` is keyed on **`slug::coupon`**. A different coupon is a
  different key, so it always gets its own attempt. If the first code was dead,
  the second is still tried.
- `ENROLLED_SLUGS` is keyed on **`slug` alone**. Once a course is owned, every
  coupon for it is moot — a second code cannot enrol you twice.

A course is therefore skipped outright **only when Udemy itself confirmed
ownership** (a completed enrollment, or `is_valid_student = true`). A dropped or
expired course is never pre-skipped and still reaches the retry queue.

| Situation | Behaviour |
|---|---|
| Same course, same coupon, 3 channels | 1 processed, 2 skipped |
| Same course, **different** coupons, not enrolled | **All tried** |
| Different coupons, second one works | First tried, second enrols, third skipped |
| Reposted next day, never enrolled | Fully reconsidered |
| Reposted next day, already owned | Skipped instantly, no browser |

### Ownership is remembered across restarts

Course ownership is the one fact here that never changes — you cannot un-own a
Udemy course — so it is persisted to `enrolled_courses.json` (git-ignored) rather
than being forgotten on every restart.

It is written **only after Udemy confirms** ownership, never on a guess, and
entries carry a 30-day expiry so that anything written in error heals itself
instead of blocking a course permanently. The store also fails open: a missing,
corrupt or unwritable file logs a warning and starts empty, and the bot simply
re-learns ownership the slower, correct way. **A cache problem can never stop
enrollment.**

## Getting Started

### Prerequisites

- Python 3.10 or newer
- A Telegram account, and API credentials from [my.telegram.org](https://my.telegram.org) (`API_ID` + `API_HASH`)
- Membership in whichever coupon-posting channels you want to monitor
- A private Telegram channel (or group) of your own to receive alerts
- A Udemy account, plus its `access_token` cookie value (from your browser's dev tools while logged into udemy.com)

### Local Setup

```bash
git clone <your-repo-url>
cd ScholarSync

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
playwright install-deps          # Linux only — installs system libraries Chromium needs

cp config/.env.example .env
# now edit .env and fill in your real API_ID, API_HASH, TARGET_CHANNELS,
# ALERT_CHANNEL_ID, and UDEMY_ACCESS_TOKEN

python main.py
```

On first run, Pyrogram/Kurigram will prompt for your phone number, a login code (sent via the Telegram app, not SMS), and your two-step verification password if enabled. This creates a local session file (`scholarsync_session.session`) so you won't need to log in again on future runs.

## Deploying to Production (Any Linux VPS)

The bot is designed to run 24/7 on a small cloud VM (built and tested on an Oracle Cloud Free Tier instance running Ubuntu 20.04, but this applies to any Linux server).

### 1. Connect via SSH / PuTTY

On Windows, use [PuTTY](https://www.putty.org/): host = your VM's IP, port 22, and your provider's private key (`.ppk` for PuTTY, or a `.pem` converted with PuTTYgen) under Connection → SSH → Auth → Credentials. On macOS/Linux, just `ssh -i your-key.pem ubuntu@<your-vm-ip>`.

### 2. Install Python 3.10+

Many older Ubuntu images (e.g. 20.04) ship with Python 3.8, which is too old. If your distro's usual PPA method (`deadsnakes`) fails — which can happen if that PPA has stopped publishing packages for your specific Ubuntu release — the most reliable fallback is Miniconda:

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda3
$HOME/miniconda3/bin/conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
$HOME/miniconda3/bin/conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
$HOME/miniconda3/bin/conda create -y -n scholarsync python=3.11
source $HOME/miniconda3/bin/activate scholarsync
```

### 3. Transfer the Project (WinSCP / SCP)

On Windows, [WinSCP](https://winscp.net/) reuses the same private key as PuTTY: File protocol SFTP, same host/port/username, and the same key file under Advanced → SSH → Authentication. Upload the whole project folder (except `venv/`, `__pycache__/`, and any `.session` files — those are local-only or get regenerated).

On macOS/Linux: `scp -i your-key.pem -r ScholarSync ubuntu@<your-vm-ip>:~/`

### 4. Install Dependencies

```bash
cd ~/ScholarSync
pip install -r requirements.txt
playwright install chromium
playwright install-deps
```

### 5. First Run & Telegram Login

Run it once in the foreground to complete Telegram authentication on the server (same phone/code/2FA prompt as local setup):

```bash
python main.py
```

Once you see it listening cleanly, `Ctrl+C` to stop it — you're ready to make it permanent.

### 6. Run It Forever with systemd

Create `/etc/systemd/system/scholarsync.service`:

```ini
[Unit]
Description=ScholarSync - Telegram Udemy Coupon Auto-Enroll Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ScholarSync
Environment="HOME=/home/ubuntu"
ExecStart=/usr/bin/xvfb-run -a /home/ubuntu/miniconda3/envs/scholarsync/bin/python /home/ubuntu/ScholarSync/main.py
Restart=always
RestartSec=15
StandardOutput=append:/home/ubuntu/ScholarSync/scholarsync.log
StandardError=append:/home/ubuntu/ScholarSync/scholarsync.log

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable scholarsync
sudo systemctl start scholarsync
```

Your bot now starts automatically on boot and restarts itself if it ever crashes — no `tmux`, no manually reopening a terminal.

Two details in that unit file matter:

- **`xvfb-run -a`** — enrollment drives a real Chrome, which needs a display. `-a`
  auto-picks a free display number so a restart never collides with a stale lock file.
- **`Environment="HOME=/home/ubuntu"`** — the browser profile lives at
  `~/.scholarsync_browser`. If `HOME` were unset, Chrome would build a fresh profile
  every run and re-solve the Cloudflare challenge each time instead of reusing its
  saved clearance.

### 7. Install Xvfb and the browser

```bash
sudo apt-get update
sudo apt-get install -y xvfb
playwright install chromium
sudo $(which playwright) install-deps
```

### 8. Supply your Udemy session cookies

Enrollment happens through Udemy's checkout page, which needs a genuinely logged-in
browser session — not just an API token. In Chrome on your own machine, open
**F12 → Application → Cookies → https://www.udemy.com** and copy these values:
`access_token`, `csrftoken`, `dj_session_id`, `ud_user_jwt`, `client_id`.
Then in the Console run `navigator.userAgent` and copy that too.

Put them in a text file and let the helper write them in for you:

```bash
python apply_cookies.py "cookies.txt" --user-id YOUR_UDEMY_USER_ID --ua "PASTE_USER_AGENT"
rm cookies.txt
```

> **Do not copy `cf_clearance` from your PC.** Cloudflare binds it to the IP address
> and User-Agent that earned it, so a clearance from your home connection is void on
> a server. The bot's browser earns its own and caches it in its profile.

## Admin Panel

A browser-based dashboard (`admin_panel/`) for operating the bot without SSH: is it
running, what did it just do, and a controlled way to rotate `.env` values (like
`UDEMY_ACCESS_TOKEN`) when a session expires. Built with Flask (server-rendered,
deliberately no separate JS frontend) and Bootstrap 5, served through Gunicorn behind
an nginx reverse proxy with HTTPS and IP allowlisting — a fully separate web
application from the bot, running as its own systemd service.

**Pages** (left-hand collapsible sidebar, all requiring login):

| Page | What it's for |
|---|---|
| Dashboard | Service status (active/inactive, uptime, restart count), RAM + swap usage, bot process memory, current log file size, last ENROLLED/DROPPED/ERROR line, one-click restart. Auto-refreshes every 5 seconds (toggle). |
| Logs | Searchable, filterable by category (enrolled / dropped / errors / warnings / retries / Cloudflare / posts) — the same categories `logs.sh` uses. Newest lines shown first. Optional 5-second auto-refresh. |
| Archives | Browse rotated `.gz` log files before they expire — same search/filter UI as Logs, but read-only and frozen (an archive's content never changes once written). |
| Environment | Edit existing `.env` keys only (never creates new ones). Values are always masked (e.g. `AbCd******xyz`); saving backs up the old file first and requires a manual restart to take effect. |
| Commands | Read-only reference for the SSH commands you'd otherwise have to remember — service control, logs, testing, the kurigram fix, the git safety check. |
| Audit | Every login and every environment/profile change — who, when, from what IP, and which key names (never values). |
| Profile | Change your own username/password, with your current password required to confirm. |

**Log lifecycle.** The live log grows until it's rotated into a compressed `.gz`
archive every 3 days (or sooner if it hits 25MB), enforced by a systemd timer since
logrotate itself has no native "every N days" period. Archives are kept for 14 days,
then deleted automatically — browsable on the Archives page the whole time they exist.

**Security model, in brief** — the full reasoning is in `ADMIN_PANEL_GUIDE.md`:

- HTTPS only, single admin account with a scrypt-hashed password (never plaintext)
- `.env` values are always masked in the UI; saving only replaces existing keys,
  never creates new ones, and every write is preceded by a timestamped backup
- Access restricted at **three** independent layers: the Oracle Cloud Security List,
  the VM's own `iptables`, and nginx's own IP allowlist — a stranger with the GitHub
  source code still can't get a single packet to the login page from outside your IP
- The panel's only power over the bot is running `systemctl status/restart` on the
  `scholarsync` service specifically, via a narrowly-scoped, auditable sudo rule —
  nothing else on the VM is reachable from it
- CSRF protection on every form, login throttling after repeated failures (5 attempts
  → 15-minute lockout per IP), and a full audit trail of logins and changes (key
  names only — values are never logged, anywhere)
- The Archives page only ever opens a file whose exact name it just generated itself
  moments earlier — never a path built from the request — closing off path traversal
- Runs as its own systemd service (`scholarsync-panel`), fully independent from the
  bot's — a panel issue can never take the pipeline down

**Deployment** is a separate step from deploying the bot itself, covered end-to-end
(including the Oracle Cloud Security List rule, the swap-file prerequisite, and the
guided `setup_panel.sh` script) in `ADMIN_PANEL_GUIDE.md`, which is git-ignored since
it documents the real VM IP — see that file directly on your machine or the VM.

## Testing & Verification

Every script below is safe to run while the bot is live, with one exception noted.

### `test_enrollment.py` — the main test

Runs one course end to end and **bypasses every policy guardrail**, so you can test
enrollment mechanics independently of your filtering rules.

```bash
xvfb-run -a python test_enrollment.py "https://www.udemy.com/course/SLUG/?couponCode=CODE"
```

It reports, in order: parsed slug/coupon → course metadata → which session cookies
were loaded → **the real post-coupon price** → the live enrollment attempt.

| Result | Meaning |
|---|---|
| `SUCCESS` | Enrolled. Verify on udemy.com under *My Learning*. |
| `ALREADY ENROLLED` | You own it — pick a different course to test with. |
| `COUPON EXPIRED / INVALID` | Udemy no longer recognises the code. Not a bug. |
| `NOT FREE — costs $X` | Coupon is valid but only partial discount. Not a bug. |
| `ERROR` | Genuine failure — read the log lines above it. |

On failure it writes `enroll_failed.png` and `enroll_failed.html` plus a dump of every
visible button and its enabled/disabled state.

> ⚠️ Don't run this at the same time as the live service if you can help it — both
> touch the same Pyrogram session file and you'll get `database is locked`.

### `apply_cookies.py` — refresh your Udemy session

Writes cookies from a text file into `.env` so you never hand-paste them.

```bash
python apply_cookies.py "cookies.txt" --user-id YOUR_UDEMY_USER_ID --ua "PASTE_YOUR_USER_AGENT"
```

Backs up the old `.env` first, replaces keys in place, and prints a masked summary.
Delete the cookie file afterwards — it holds live credentials.

### `diagnose.py` — one-pass health check

```bash
python diagnose.py
```

Confirms your channel IDs resolve, the scraper still works against the coupon sites,
and your Udemy token is valid. Run this first whenever something feels broken.

### `debug_listener.py` — is Telegram actually pushing updates?

```bash
python debug_listener.py     # stop the service first
```

Prints every raw update as it arrives. If posts appear in your channel but nothing
prints here, the problem is the Telegram layer, not the enrollment layer.

### `test_pipeline.py` — replay a real post

```bash
python test_pipeline.py      # stop the service first
```

Pulls the latest real post from each channel and walks it through keyword match →
scraper → metadata → policy → enrollment, printing each stage. Useful when a specific
post didn't behave as expected.

### Watching the live service

```bash
sudo systemctl status scholarsync          # is it running?
tail -f /home/ubuntu/scholarsync/scholarsync.log
journalctl -u scholarsync -n 50 --no-pager  # crashes / startup errors
```

## Troubleshooting & Lessons Learned

Everything below actually happened while building this. Each one cost real hours, so
they are written down in the hope they save yours.

### Telegram

**Live updates never arrive, but login works fine.**
The original `pyrogram` package has been unmaintained since 2023 and silently stopped
handling newer Telegram protocol layers — you can log in and call methods, but no
push updates are delivered. Fix: `pip uninstall pyrogram && pip install kurigram`.
Same import name, no code changes.

**`RuntimeError: Task attached to a different loop`.**
`asyncio.run()` creates a *new* event loop, while a module-level `Client` binds to the
one that existed when it was constructed. Use
`loop = asyncio.get_event_loop(); loop.run_until_complete(main())`.

**Python 3.11 won't install from the deadsnakes PPA.**
Its package index for that Ubuntu release was empty — the giveaway was
`d41d8cd98f00b204e9800998ecf8427e` (the MD5 of an empty file) in the PPA metadata.
Fix: install Miniconda and `conda create -n scholarsync python=3.11`.

### Enrollment — the seven bugs

These are listed in the order they were found. The first three meant **no course could
ever enroll**, and each one masked the ones behind it.

**1 · The price guardrail was coupon-blind.**
`GET /api-2.0/courses/{slug}/` ignores any coupon you pass it and only ever returns the
list price. That price was fed into the "is it free?" check, so every genuinely-free
course was labelled `Coupon expired, price=$19.99` and dropped before enrollment was
even attempted. Fix: `get_coupon_pricing()` calls
`/course-landing-components/{id}/me/?couponCode=…`, which actually evaluates the coupon
and also reports ownership in the same round-trip.

**2 · Brand-new and practice-test courses could never pass.**
An unrated course reports `rating: 0.0`, and a practice-test course reports
`0.0` hours because it has no video. Both were being rejected — yet those are exactly
the courses free coupons promote. Fix: treat `0.0` rating as *unrated* rather than
*bad*, and exempt zero-duration courses from the duration rule.

**3 · The enrollment browser was silently logged out.**
`access_token` authenticates Udemy's JSON API, which is why metadata lookups worked.
But `/payment/checkout/` is a server-rendered Django page that needs `dj_session_id`
and `ud_user_jwt` as well. Without them the checkout rendered
*"1. Log in or create an account"* and could never complete.

**4 · Wrong query parameter.**
The checkout URL was built with `?discountCode=`. Udemy uses `?couponCode=`. With the
wrong name the coupon was silently ignored and the cart stayed at full price.

**5 · Cloudflare cookies cannot be copied between machines.**
`cf_clearance` is bound to both the IP address *and* the exact User-Agent that earned
it. A clearance from a home connection is worthless on a server — and injecting it is
worse than sending nothing, because Cloudflare sees a clearance issued to a different
address. Fix: run Chrome under Xvfb with a persistent profile so it earns its own, and
stop overriding the User-Agent (a desktop Chromium advertising an Android phone is an
instant bot signal).

**6 · A completed enrollment was reported as an error.**
Udemy's *express* checkout finalises a 100%-off order on page load — frequently with no
button to press at all. The code searched for an "Enroll now" button, didn't find one,
and returned `ERROR` while the course had in fact been enrolled. Compounding it, the
diagnostic `page.screenshot()` hung on *"waiting for fonts to load"* and raised an
uncaught timeout that crashed the whole run. Fix: the ownership API is now the deciding
authority at every exit, and screenshots can never affect the outcome.

**7 · `.first` clicked an invisible button.**
Udemy renders ~17 buttons including hidden desktop/mobile duplicates, so
`locator('button:has-text("Enroll now")').first` resolved to a **hidden** copy. The code
waited for it to become visible, gave up, and moved on — while the real button sat
untouched. Fix: find and click the button inside the page with a single
`page.evaluate()`, choosing the first element that is genuinely visible and enabled.

### Performance

**A single enrollment took 14 minutes.**
Two causes. Every Playwright locator call (`is_visible`, `is_enabled`, `inner_text`) is
a separate round-trip to Chromium, costing several seconds each on a small VM — walking
8 selectors × 8 elements burned ~94 seconds before the first click. And Udemy's own
`{"status":"succeeded"}` response was being logged and then ignored, wasting another
~24 seconds. Doing the search in one `page.evaluate()` and trusting Udemy's confirmation
brought it to **~55 seconds**.

**Playwright's normal `click()` never works on that button.**
It fails actionability with *"waiting for element to be visible, enabled and stable"* —
Udemy animates it, so it is never "stable". A JS click is the only reliable route.

### Operational

**Two enrollments at once break both.** `main.py` dispatches work through
`run_in_executor`, so simultaneous posts land in separate threads. Both would launch
Chrome against the same profile directory, which Chrome refuses. A `threading.Lock`
serialises them.

**Never run the sync Playwright API inside the asyncio loop.** It must be called from a
worker thread — which `run_in_executor` provides.

## What Gets Pushed to GitHub

### Push these

| File | Why |
|---|---|
| `main.py` | The bot |
| `utils/*.py` | Core package (filter, scraper, udemy, enroll_browser, notifier, retry_queue) |
| `apply_cookies.py` | Cookie helper — handles no secrets itself |
| `test_enrollment.py`, `test_pipeline.py`, `debug_listener.py`, `diagnose.py` | Diagnostics; genuinely useful to anyone deploying this |
| `poller_main.py`, `utils/poller.py` | The abandoned polling architecture, kept deliberately as a record of what didn't work |
| `requirements.txt` | Dependencies |
| `config/.env.example` | Placeholders only |
| `README.md`, `.gitignore` | This file, and the rules below |
| `admin_panel/*.py`, `templates/`, `static/`, `deploy/*` | Admin panel source — no secrets in any of it |
| `admin_panel/requirements.txt`, `admin_panel/.env.example` | Panel dependencies and a placeholders-only template |

### Never push these

| File | Why |
|---|---|
| `.env`, `.env.backup-*` | **Live credentials** — Telegram API hash, Udemy session cookies (this pattern also catches `admin_panel/.env`) |
| `cookies.txt` / any `*cookies*.txt` | Raw cookie dumps straight from DevTools |
| `*.session`, `*.session-journal` | **Your Telegram login.** Anyone with this file *is* you on Telegram |
| `scholarsync.log`, `admin_panel/audit.log` | Contains course titles, coupon codes, timestamps, and admin activity |
| `retry_queue.json`, `seen_courses.json` | Runtime state, regenerates itself |
| `enroll_*.png`, `enroll_failed.html` | Debug captures of a logged-in session |
| `ORACLE_DEPLOYMENT_GUIDE.md`, `ADMIN_PANEL_GUIDE.md`, `PROGRESS_REPORT.md`, `IMPLEMENTATION_PLAN.md`, `task.md` | Personal working notes containing the real server IP |
| `__pycache__/`, `venv/` (also catches `admin_panel/venv/`) | Build artefacts |

All of the above are already listed in `.gitignore`. **Verify before your first push:**

```bash
git status --short          # nothing secret should be listed
git ls-files | grep -E '\.env$|session|cookies'   # must print nothing
```

If that second command prints anything, stop and fix `.gitignore` before pushing —
rewriting git history after the fact is far harder than not committing it.

## Roadmap

- [ ] Configurable retry-queue interval/duration via `.env`
- [x] Web-based admin dashboard (status, logs, env editor, service control) — see [Admin Panel](#admin-panel)
- [ ] Real domain + Let's Encrypt HTTPS for the admin panel (currently self-signed)
- [ ] Support additional coupon-aggregator source sites
- [ ] Per-category alert formatting/emoji customization

## Security Notes

- **`.env` is git-ignored on purpose.** It holds your Telegram API credentials and Udemy access token. Never commit it, never paste its contents anywhere public.
- **`*.session` files are equally sensitive** — they represent an active, logged-in Telegram session. Treat them like a password.
- **Respect `robots.txt`** on any site you point the scraper at. This project's scraper avoids fetching pages disallowed by a site's `robots.txt`.
- Rotate your Udemy `access_token` if you ever suspect it's been exposed (`udemy.com` → F12 → Application → Cookies → `access_token`).

## Disclaimer

This is a personal automation project built for educational/personal use. Web scraping and API automation may be subject to the terms of service of the sites and services involved (Telegram, Udemy, and any coupon-aggregator sites) — review and comply with those terms for your own use case. Use responsibly and at your own risk.

## License

[MIT](LICENSE) — free to use, modify, and share. (Add a `LICENSE` file with the MIT text, or swap this for whichever license you prefer, before publishing.)
