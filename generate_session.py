"""
generate_session.py
-------------------
Helper script to generate a Pyrogram string session.
Run locally:
    python generate_session.py

It prints your SESSION_STRING which you can paste directly into Render Environment Variables.
"""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()


async def main():
    try:
        from pyrogram import Client
    except ImportError:
        print("Please install kurigram: pip install kurigram")
        return

    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")

    if not api_id or not api_hash:
        print("Enter your Telegram API credentials from https://my.telegram.org:")
        api_id = input("API_ID: ").strip()
        api_hash = input("API_HASH: ").strip()

    print("\nConnecting to Telegram to generate your session string...")
    async with Client(":memory:", api_id=int(api_id), api_hash=api_hash) as app:
        session_str = await app.export_session_string()
        print("\n" + "=" * 60)
        print("YOUR TELEGRAM SESSION STRING (Copy this for Render):")
        print("=" * 60)
        print(session_str)
        print("=" * 60)
        print("\nIn Render dashboard, add environment variable:")
        print("SESSION_STRING = <the string above>\n")


if __name__ == "__main__":
    asyncio.run(main())
