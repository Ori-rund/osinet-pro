"""
התחברות חד-פעמית לטלגרם → מחרוזת session.

להריץ פעם אחת על המחשב שלך (לא על השרת):

    python login_telegram.py

תתבקש להזין מספר טלפון, ואז קוד שיגיע לטלגרם. בסוף תקבל מחרוזת
ארוכה — זה TELEGRAM_SESSION. שים אותה כמשתנה סביבה ב-Railway.

המחרוזת הזו היא גישה מלאה לחשבון הטלגרם שלך. לא ב-git, לא בצ'אט,
לא בצילום מסך. אם דלפה — Telegram → Settings → Devices → Terminate.
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()


async def main() -> None:
    api_id = os.getenv("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("TELEGRAM_API_HASH: ").strip()

    async with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        me = await client.get_me()
        print("\n" + "=" * 64)
        print(f"מחובר כ: {me.first_name} (@{me.username})" if me.username
              else f"מחובר כ: {me.first_name}")
        print("=" * 64)
        print("\nTELEGRAM_SESSION=" + client.session.save())
        print("\n⚠️  זו גישה מלאה לחשבון. רק כמשתנה סביבה, לעולם לא ב-git.\n")


if __name__ == "__main__":
    asyncio.run(main())
