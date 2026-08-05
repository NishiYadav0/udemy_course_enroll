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
- [Verifying It's Working](#verifying-its-working)
- [Troubleshooting & Lessons Learned](#troubleshooting--lessons-learned)
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
 Telegram Channel Post
         │
         ▼
 ┌───────────────────┐
 │ 1. LISTEN          │  Pyrogram/Kurigram userbot receives the live post
 └────────┬───────────┘
          ▼
 ┌───────────────────┐
 │ 2. CATEGORIZE       │  Keyword match → data_science / coding / other / ...
 └────────┬───────────┘
          ▼
 ┌───────────────────┐
 │ 3. SCRAPE           │  Visit the intermediary site (or read the button URL),
 │                     │  extract every Udemy coupon link on the page
 │                     │  (static fetch first, headless-browser fallback for
 │                     │  JavaScript-only pages)
 └────────┬───────────┘
          ▼
 ┌───────────────────┐
 │ 4. METADATA          │  Query the Udemy API for price, duration, rating,
 │                     │  language, and badges
 └────────┬───────────┘
          ▼
 ┌───────────────────┐
 │ 5. POLICY CHECK     │  Language OK? Coupon actually free? Rating ≥ 4.0?
 │                     │  Duration meets the category's minimum (or popular)?
 └────────┬───────────┘
      pass │  fail → queued for a coupon retry (if eligible) or dropped
          ▼
 ┌───────────────────┐
 │ 6. ENROLL            │  POST to Udemy's enrollment endpoint
 └────────┬───────────┘
          ▼
 ┌───────────────────┐
 │ 7. NOTIFY            │  Confirmation message sent to your alert channel
 └───────────────────┘
```

## Tech Stack

| Library | Role |
|---|---|
| [Kurigram](https://github.com/KurimuzonAkuma/pyrogram) | Telegram MTProto client (userbot) — a maintained fork of the original Pyrogram, needed because the original stopped receiving updates for newer Telegram protocol layers |
| [Playwright](https://playwright.dev/python/) | Headless-browser fallback for scraping JavaScript-rendered coupon pages |
| [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) | HTML parsing for link extraction |
| [Requests](https://requests.readthedocs.io/) | Fast static page fetching (tried before falling back to a full browser) |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Loads secrets from a local `.env` file |

Python **3.10+** is required (the codebase uses modern type-hint syntax like `str | None`).

## Project Structure

```
ScholarSync/
├── main.py                    # Entry point — the bot itself
├── requirements.txt           # Python dependencies
├── .env                       # Your secrets (git-ignored, never commit this)
├── config/.env.example        # Template showing what .env should contain
├── utils/
│   ├── filter.py              # Category keywords + enrollment policy rules
│   ├── scraper.py             # Intermediary-site scraping + Udemy link extraction
│   ├── udemy.py                # Udemy API calls (metadata fetch + enrollment)
│   ├── notifier.py            # Alert message formatting
│   └── retry_queue.py         # Coupon-retry queue for expired-coupon courses
├── poller_main.py              # Alternate entry point: website-polling instead
│                                 of Telegram (kept for reference — see
│                                 Troubleshooting for why it wasn't the final choice)
├── utils/poller.py            # Supporting code for poller_main.py
├── debug_listener.py          # Diagnostic: proves whether live Telegram
│                                 updates are actually being received
├── test_pipeline.py           # Diagnostic: manually runs one real post through
│                                 the full pipeline for step-by-step debugging
└── diagnose.py                 # Diagnostic: checks channel IDs, scraper, and
                                  Udemy token validity in one pass
```

## Enrollment Policy

A course is only enrolled if it clears **every** rule below, checked in this order:

1. **Language** — must be English or Hindi.
2. **Paid course** — natively free courses are skipped (this tool targets coupon-gated paid courses).
3. **Working coupon** — checkout price with the coupon applied must actually be $0 (not just claimed "100% off" in the post).
4. **Rating** — must be ≥ 4.0★.
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

All of this is defined in one place — `utils/filter.py` — so tuning the rules to your own goals is a one-file edit.

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
ExecStart=/home/ubuntu/miniconda3/envs/scholarsync/bin/python /home/ubuntu/ScholarSync/main.py
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

## Verifying It's Working

```bash
sudo systemctl status scholarsync        # Is it running?
tail -n 100 scholarsync.log              # Last 100 lines
tail -f scholarsync.log                  # Watch live (Ctrl+C to stop watching — the bot itself keeps running)
grep -i "keyword" scholarsync.log        # Search for a specific course/event
```

All of the above are read-only and 100% safe to run at any time — they only display the log, never modify anything.

A healthy startup log looks like:
```
Warming up peer cache for N channel(s)...
  ✅ Cached: <channel name>  (id=...)
Syncing dialogs (required for live channel updates)...
✅ Dialog sync complete — N dialog(s) synced.
🚀 Peer cache ready — listening for new posts...
```
followed by a "💚 Bot alive" heartbeat line every 10 minutes, and a full pipeline trace (`✉️ New post` → `🔍 Resolving...` → `Policy result: ...`) every time a monitored channel posts.

## Troubleshooting & Lessons Learned

Real problems hit (and fixed) while building this — kept here so the next person doesn't have to rediscover them the hard way:

**"The bot runs but never reacts to new posts" (silent, no errors).** Two separate causes were found, both fixed:
- `get_chat()` alone only resolves a channel's peer info — it does *not* register the channel for live update delivery. A full `get_dialogs()` pass after login is required once per session for Telegram to actually start pushing new-message updates for that channel.
- Even with dialog sync correct, the original `pyrogram` library (unmaintained since 2023) can silently stop receiving live updates entirely, while every other call (login, `get_dialogs`, `get_me`) still works fine — a very misleading failure mode. Switching to [Kurigram](https://github.com/KurimuzonAkuma/pyrogram), a maintained fork with the exact same import name (`pyrogram`), fixed it immediately with zero code changes elsewhere.

**`RuntimeError: ... attached to a different loop`.** Caused by mixing `asyncio.run()` (which creates a brand-new event loop) with a Pyrogram/Kurigram `Client` that was constructed — and bound its internal loop — earlier at module scope. Fix: use `asyncio.get_event_loop()` + `loop.run_until_complete(...)` consistently instead of `asyncio.run()`.

**Coupon "Enroll Now" links inside Telegram buttons, not message text.** Some channels put the actual link only in an inline keyboard button (`message.reply_markup`), not the post's visible text. A dedicated button-URL extractor was needed alongside the text-URL extractor.

**Some pages are JavaScript-only.** A plain `requests.get()` returns an empty shell for client-side-rendered pages. A headless-browser fallback (Playwright) renders these correctly — but only launched when the fast static fetch finds nothing, to keep memory usage low on a small VM.

**A PPA can silently stop supporting your OS version.** `deadsnakes` (the standard way to get newer Python on older Ubuntu) can end up serving a technically-valid-but-empty package index for a given release — `apt` reports no error, it just never finds the package. Verified by inspecting the PPA's raw `InRelease` file directly; worked around with Miniconda instead of fighting the PPA.

**A single scrape can miss a coupon added later.** Confirmed directly: a course page's own "last updated" timestamp was 16 minutes *after* the bot had already scraped it and found only an expired coupon. No amount of smarter parsing fixes this — it needs an actual second look later, which is what the retry queue (`utils/retry_queue.py`) does.

**Server clock ≠ your local time.** Cloud VMs default to UTC. Fix once with `sudo timedatectl set-timezone <Your/Timezone>`.

## Roadmap

- [ ] Configurable retry-queue interval/duration via `.env`
- [ ] Optional web dashboard for enrollment history
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
