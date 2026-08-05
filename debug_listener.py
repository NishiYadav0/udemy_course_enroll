"""
debug_listener.py
------------------
Temporary diagnostic listener — run this INSTEAD of main.py.

Unlike main.py, this prints EVERY channel message your account
receives live (not just the two target channels), tagging whether
each one is a target channel or not. Let it run for a few minutes
while target channels are actively posting.

WHAT TO LOOK FOR:
  - If NOTHING prints at all, even from totally unrelated channels
    you're in, the live push connection itself is broken (network /
    Pyrogram issue, not a channel-specific problem).
  - If OTHER channels print live messages but your two target
    channels never do (even though you can see with your own eyes
    that they're posting), it's a membership/dialog-sync problem
    specific to those two channels — Telegram is not pushing you
    updates for them even though you can view them.
  - If a target channel message DOES print here, then main.py has a
    bug in its own handler/filter logic, not a connectivity problem.

Run:
    python debug_listener.py
Stop with Ctrl+C once you've seen enough (or after a few minutes).

Do not run this at the same time as main.py or test_pipeline.py —
they all share the same session file and SQLite will lock.
"""

import asyncio
import os
import time

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import Message

load_dotenv()

API_ID   = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
TARGET_CHANNELS = set(
    int(x.strip()) for x in os.getenv("TARGET_CHANNELS", "").split(",") if x.strip()
)

app = Client("scholarsync_session", api_id=API_ID, api_hash=API_HASH)

# Shared counters so the heartbeat can report what's actually happened
_stats = {"raw_updates": 0, "channel_messages": 0, "any_messages": 0}


@app.on_message(filters.channel)
async def any_channel_message(client: Client, message: Message) -> None:
    _stats["channel_messages"] += 1
    tag = "🎯 TARGET CHANNEL" if message.chat.id in TARGET_CHANNELS else "  (other channel)  "
    title = (message.chat.title or "?")[:40]
    text = (message.text or message.caption or "[no text]")[:60]
    print(f"{tag} | id={message.chat.id:<16} | {title:<40} | msg={message.id} | {text!r}")


@app.on_message(filters.all)
async def literally_any_message(client: Client, message: Message) -> None:
    # Catches private chats, groups, supergroups, channels — everything.
    _stats["any_messages"] += 1


@app.on_raw_update()
async def raw_update_catchall(client: Client, update, users, chats) -> None:
    # This fires on ANY MTProto event Telegram sends your account at
    # all — new messages, edits, read receipts, status changes, typing
    # indicators, etc. It bypasses Pyrogram's message-parsing layer
    # entirely. If this counter stays at 0, the live connection itself
    # is not receiving traffic from Telegram — full stop, not a
    # channel-specific or filter-specific issue.
    _stats["raw_updates"] += 1
    if _stats["raw_updates"] <= 5:
        print(f"  [raw update #{_stats['raw_updates']}] {type(update).__name__}")


async def _heartbeat() -> None:
    start = time.monotonic()
    while True:
        await asyncio.sleep(15)
        elapsed = int(time.monotonic() - start)
        print(
            f"⏱  {elapsed}s elapsed | raw_updates={_stats['raw_updates']} | "
            f"any_messages={_stats['any_messages']} | "
            f"channel_messages={_stats['channel_messages']}"
        )


async def main() -> None:
    async with app:
        print("Listening for ANY live update... (Ctrl+C to stop)")
        print("Target channels being watched for:", TARGET_CHANNELS)
        print("-" * 70)
        async for dialog in app.get_dialogs():
            pass  # sync dialogs the same way main.py does, for a fair test
        print("Dialog sync done. Now watching live traffic...")
        print("A heartbeat with live counters will print every 15 seconds.")
        print("-" * 70)
        asyncio.get_event_loop().create_task(_heartbeat())
        await idle()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
