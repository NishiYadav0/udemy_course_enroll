[← Back to README](../README.md) · [← Chapter 1 — Deployment](DEPLOYMENT_GUIDE.md)

# 🖥️ Chapter 2 — Front-End Setup (Telegram, Channels, Udemy — zero SSH)

Everything from here happens in the browser, at `https://<your-ip>/` from
[Chapter 1](DEPLOYMENT_GUIDE.md) — no more terminal.

## 2.1 Claim your admin account

Click **"Create your admin account"** on the login page, pick a username and
a password (12+ characters), and submit. You're logged in immediately and
dropped into the setup wizard.

## 2.2 Get your Telegram API ID and Hash

Before the wizard's Telegram step, open a second tab to
[my.telegram.org](https://my.telegram.org), log in with your own phone
number, go to **API development tools**, and create an app (any name/platform
is fine — this is just how Telegram issues you API credentials). You'll get:

- **API ID** — a plain number, e.g. `12345678`
- **API Hash** — a 32-character string

These identify your app to Telegram, not your account specifically — free,
instant, and yours to keep.

## 2.3 Log in to Telegram from the wizard

Back in the panel: **Setup → 1. Log in to Telegram**. Paste your API ID, API
Hash, and phone number (international format, e.g. `+91XXXXXXXXXX`) and
submit. Telegram sends a login code to your **Telegram app itself** (check
there, not SMS, unless Telegram falls back to it) — type that code in. If
your account has Two-Step Verification enabled, you'll get one more prompt
for that password. That's it — this is the exact same login `main.py` used to
ask for over an SSH terminal, just as a web form instead.

## 2.4 Add your target channels

**Setup → 2. Pick channels**. You must already be a member of whichever
channels you want ScholarSync to watch (join them in the Telegram app first
if you haven't). Type each channel's `@username` (or paste its `t.me/...`
link) into the "Add a target channel" box — the panel looks up its real
numeric chat ID for you automatically (main.py requires the numeric ID
internally; you never have to find it by hand). Repeat for every channel you
want monitored.

## 2.5 Set your alert channel

Same page, second box. This should be **your own private channel or group**
(create one in Telegram first if you don't have one) — not one of the coupon
channels above. ScholarSync posts enrollment confirmations here, so it needs
to be somewhere only you (or people you trust) can see.

## 2.6 Add your Udemy account

**Setup → 3. Add Udemy**. Enrollment needs a genuinely logged-in Udemy
browser session, not just an API key:

1. Log in to [udemy.com](https://www.udemy.com) in Chrome
2. Press **F12** → **Application** tab → **Storage → Cookies** →
   `https://www.udemy.com`
3. Copy each of these values into the matching field in the wizard:

| Cookie name in DevTools | Wizard field | Required? |
|---|---|---|
| `access_token` | access_token | ✅ Yes |
| `csrftoken` | csrftoken | ✅ Yes |
| `dj_session_id` | dj_session_id | ✅ Yes |
| `ud_user_jwt` | ud_user_jwt | ✅ Yes |
| `client_id` | client_id | Optional |
| `ud_cache_user` | your numeric user id | Optional |
| `cf_clearance` | cf_clearance | Optional, helps with Cloudflare |
| `__cf_bm` | __cf_bm | Optional, short-lived (~30 min) |

For the **User-Agent** field, open DevTools' **Console** tab (same browser,
same tab) and type `navigator.userAgent`, then paste the result exactly —
this has to match the browser that captured `cf_clearance` character for
character or Cloudflare rejects it.

> The four marked required are enough for enrollment to work. The rest help
> the bot get past Cloudflare's bot detection on the checkout page — you can
> leave them blank now and add them later from the Environment page if you
> hit Cloudflare issues.

## 2.7 Review and launch

**Setup → Review & launch** shows a masked summary of everything above.
Click **"Write config & launch the bot"** — this writes the real `.env` and
starts the `scholarsync` service for the first time (the equivalent of
`systemctl enable --now scholarsync` from Chapter 1, run automatically). The
Dashboard takes over from here — service status, memory, live logs, and (from
the [Categories](../README.md#admin-panel) page) full control over what kind
of courses actually get enrolled.

---

## Appendix — Every `.env` key explained

The wizard writes all of these for you the first time, but you'll see this
exact list again on the panel's **Environment** page any time you need to
rotate one later (e.g. after a Udemy cookie expires). Here's what each key
actually is and exactly where it comes from:

| Key | What it is | Where to get it | Required? |
|---|---|---|---|
| `API_ID` | Your Telegram app's API ID — a plain number | [my.telegram.org](https://my.telegram.org) → **API development tools** → create an app | ✅ Yes |
| `API_HASH` | Your Telegram app's API Hash — a 32-character string | Same page as API_ID, right next to it | ✅ Yes |
| `TARGET_CHANNELS` | Numeric chat IDs (comma-separated) of the Telegram channels ScholarSync watches for coupon posts | Set automatically by the wizard's **Channels** step — type `@channelname`, it resolves the real ID for you | ✅ Yes |
| `ALERT_CHANNEL_ID` | Numeric chat ID of your own private channel/group where enrollment confirmations get sent | Same wizard step, second box — create a private channel first if you don't have one | ✅ Yes |
| `UDEMY_ACCESS_TOKEN` | Udemy's `access_token` cookie — authenticates API calls (course lookups, ownership checks) | udemy.com (logged in) → **F12 → Application → Cookies → https://www.udemy.com** → `access_token` | ✅ Yes |
| `UDEMY_CSRF_TOKEN` | Udemy's `csrftoken` cookie — required for any write/POST request Udemy's API makes | Same DevTools panel → `csrftoken` | ✅ Yes |
| `UDEMY_DJ_SESSION_ID` | Udemy's `dj_session_id` cookie — without it the checkout page renders as logged-out, no matter how valid the access token is | Same → `dj_session_id` | ✅ Yes |
| `UDEMY_USER_JWT` | Udemy's `ud_user_jwt` cookie — also required specifically for the checkout page to recognise you | Same → `ud_user_jwt` | ✅ Yes |
| `UDEMY_CLIENT_ID` | Udemy's `client_id` cookie | Same → `client_id` | Optional |
| `UDEMY_USER_ID` | Your numeric Udemy account ID | Same → `ud_cache_user` | Optional |
| `UDEMY_CF_CLEARANCE` | Cloudflare's `cf_clearance` cookie — helps the enrollment browser get past Cloudflare's bot check on the checkout page | Same → `cf_clearance`. **Must come from the same machine/IP** that will actually run the bot — a clearance captured on your home PC won't work on the VM | Optional, recommended |
| `UDEMY_CF_BM` | Cloudflare's `__cf_bm` cookie — short-lived, expires roughly every 30 minutes | Same → `__cf_bm` | Optional |
| `UDEMY_USER_AGENT` | The exact User-Agent string of the browser you captured the cookies above in | Same browser, DevTools **Console** tab → type `navigator.userAgent` → copy the result exactly | Optional, but required if you set `UDEMY_CF_CLEARANCE` — it's checked character-for-character |
| `POLL_INTERVAL_SECONDS` | How often (in seconds) the alternate *website-polling* mode checks for new coupons | Only read by `poller_main.py`, the abandoned polling-based architecture kept for reference — the real bot (`main.py`) listens to Telegram live and ignores this entirely. Safe to leave at its default | Not used in normal operation |

---

[← Back to README](../README.md) · [← Chapter 1 — Deployment](DEPLOYMENT_GUIDE.md)
