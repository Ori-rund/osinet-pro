"""
קליטת ערוצי טלגרם ציבוריים דרך MTProto (Telethon).

למה לא Bot API: בוט רואה רק ערוצים שהוא אדמין בהם. לקריאת ערוץ
ציבורי שלא שלך צריך חשבון משתמש — וזה MTProto.

שני מצבים:
  live    — מאזין בזמן אמת, הודעה נכנסת לאתר תוך שניות
  backfill — מושך את ההודעות האחרונות מכל ערוץ בעלייה, כדי
             שהפיד לא יהיה ריק אחרי ריסטארט

אזהרה תפעולית: חשבון שמצטרף להרבה ערוצים במכה אחת נתפס כספאם
ועלול לחטוף הגבלה. ההצטרפות מדורגת בכוונה — אל תוריד את ההשהיה.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timezone

from telethon import TelegramClient, events, utils
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession

from config import settings
from enrich import enrich
from store import active_sources, log_run, mark_fetched, save

log = logging.getLogger("telegram")

JOIN_DELAY_SEC = 4          # השהיה בין ערוצים — הגנה מפני הגבלת ספאם
RESUBSCRIBE_SEC = 900       # רענון רשימת הערוצים כל 15 דקות


def _handle(source: dict) -> str | None:
    """מחלץ שם ערוץ מ-url או מ-handle. מחזיר None אם אי אפשר."""
    raw = (source.get("handle") or source.get("url") or "").strip()
    if not raw:
        return None
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "t.me/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.removeprefix("s/").removeprefix("@").split("/")[0].split("?")[0]
    return raw or None


def _build_item(source: dict, message) -> dict | None:
    text = (message.message or "").strip()
    if len(text) < 12:
        return None  # תמונה בלי כיתוב, סטיקר, "👍" — אין מה לעשות עם זה

    handle = _handle(source)
    when = message.date.astimezone(timezone.utc) if message.date else None
    published = when.isoformat() if when else None
    enriched = enrich(
        text,
        default_severity=source.get("default_severity") or "low",
        when=when,
    )

    return enriched | {
        "source_id": source["id"],
        "source_name": source.get("name"),
        "source_type": "telegram",
        "source_url": f"https://t.me/{handle}/{message.id}" if handle else None,
        "external_id": message.id,
        "published_at": published,
        "raw": {"channel": handle, "message_id": message.id},
    }


async def backfill(client: TelegramClient, sources: list[dict]) -> None:
    """מושך הודעות אחרונות מכל ערוץ — ממלא את הפיד בעלייה."""
    for source in sources:
        handle = _handle(source)
        if not handle:
            continue
        counts = {"inserted": 0, "deduped": 0, "skipped": 0}
        try:
            async for message in client.iter_messages(handle, limit=settings.backfill_limit):
                item = _build_item(source, message)
                if item:
                    outcome = save(item)
                    counts[outcome] = counts.get(outcome, 0) + 1
        except FloodWaitError as exc:
            log.warning("FloodWait %ss · %s — ממתין", exc.seconds, handle)
            await asyncio.sleep(exc.seconds + 2)
            continue
        except (ChannelPrivateError, UsernameNotOccupiedError, ValueError) as exc:
            log.warning("ערוץ לא נגיש · %s · %s", handle, exc)
            mark_fetched(source["id"], ok=False, error=str(exc))
            log_run(source["id"], ok=False, error=str(exc))
            continue
        except Exception as exc:
            log.error("backfill נכשל · %s · %s", handle, exc)
            continue

        mark_fetched(source["id"], ok=True)
        log.info("backfill · %-20s · %s", handle[:20], counts)
        await asyncio.sleep(JOIN_DELAY_SEC)


async def run() -> None:
    """מאזין חי. חוסם לנצח — זה התהליך הראשי של הוורקר."""
    if not settings.telegram_ready:
        log.warning("טלגרם לא מוגדר (חסר API_ID/API_HASH/SESSION) — מדלג")
        return

    client = TelegramClient(
        StringSession(settings.telegram_session),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start()
    me = await client.get_me()
    log.info("טלגרם מחובר כ-%s", me.username or me.phone or me.id)

    # ממופה מחדש בכל רענון, כדי שכיבוי מקור באתר ייכנס לתוקף
    channel_map: dict[int, dict] = {}

    async def refresh() -> list[str]:
        nonlocal channel_map
        sources = [s for s in active_sources("telegram")]
        handles, new_map = [], {}
        for source in sources:
            handle = _handle(source)
            if not handle:
                continue
            try:
                entity = await client.get_entity(handle)
                # get_peer_id נותן את אותו מזהה ש-event.chat_id יחזיר
                # (הצורה -100xxxxxxxxx לערוצים). התאמה ידנית כאן שוברת.
                new_map[utils.get_peer_id(entity)] = source
                handles.append(handle)
            except FloodWaitError as exc:
                log.warning("FloodWait %ss בפתרון %s", exc.seconds, handle)
                await asyncio.sleep(exc.seconds + 2)
            except Exception as exc:
                log.warning("לא ניתן לפתור ערוץ · %s · %s", handle, exc)
                mark_fetched(source["id"], ok=False, error=str(exc))
            await asyncio.sleep(0.6)
        channel_map = new_map
        return handles

    handles = await refresh()
    if not handles:
        log.warning("אין ערוצי טלגרם פעילים בטבלת sources")
        return
    log.info("מאזין ל-%d ערוצים", len(handles))

    await backfill(client, list(channel_map.values()))

    @client.on(events.NewMessage())
    async def on_message(event):
        source = channel_map.get(event.chat_id)
        if not source:
            return
        item = _build_item(source, event.message)
        if not item:
            return
        try:
            outcome = save(item)
            log.info("%-6s %-9s %-12s %s", outcome, item["severity"],
                     (item.get("location_name") or "—")[:12], item["title"][:60])
        except Exception as exc:
            log.error("שמירה נכשלה · %s", exc)

    async def periodic_refresh():
        while True:
            await asyncio.sleep(RESUBSCRIBE_SEC)
            try:
                await refresh()
            except Exception as exc:
                log.warning("רענון מקורות נכשל · %s", exc)

    asyncio.create_task(periodic_refresh())
    await client.run_until_disconnected()
