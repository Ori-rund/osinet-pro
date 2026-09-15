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
from relevance import screen
from store import active_sources, log_run, mark_fetched, save

log = logging.getLogger("telegram")

JOIN_DELAY_SEC = 4          # השהיה בין ערוצים — הגנה מפני הגבלת ספאם
RESUBSCRIBE_SEC = 300       # רענון רשימת הערוצים — ערוץ שנוסף באתר נקלט תוך כ-5 דקות
POLL_SEC = 180              # סריקה יזומה של הערוצים, ראה הערה ב-periodic_poll
POLL_LIMIT = 8              # הודעות אחרונות לערוץ בכל סריקה


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
        counts = {"inserted": 0, "deduped": 0, "skipped": 0, "filtered": 0}
        try:
            async for message in client.iter_messages(handle, limit=settings.backfill_limit):
                item = _build_item(source, message)
                if not item:
                    continue
                keep, reason, item = screen(item)
                if not keep:
                    counts["filtered"] = counts.get("filtered", 0) + 1
                    continue
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

    # "Incorrect padding" הוא שגיאת base64 של Telethon, והוא לא מסגיר
    # מה באמת קרה. כמעט תמיד המחרוזת נחתכה בהעתקה או שנדבק בה רווח.
    # עדיף לזהות את זה כאן ולומר מה לבדוק, מאשר להחזיר traceback.
    raw = (settings.telegram_session or "").strip()
    cleaned = "".join(raw.split())        # מסיר רווחים ושורות חדשות
    if cleaned != raw:
        log.warning("ב-TELEGRAM_SESSION היו רווחים או שורות — נוקו")
    if len(cleaned) < 200:
        log.error(
            "TELEGRAM_SESSION קצר מדי (%d תווים). מחרוזת תקינה היא "
            "כ-350 תווים — כנראה נחתכה בהעתקה. העתק אותה מחדש במלואה.",
            len(cleaned))
        return
    try:
        session = StringSession(cleaned)
    except Exception as exc:
        log.error(
            "TELEGRAM_SESSION לא ניתנת לפענוח (%s). המחרוזת פגומה — "
            "הרץ שוב את שלב ההתחברות והעתק את הפלט במלואו, בלי "
            "רווחים ובלי לחתוך את הסוף.", exc)
        return

    client = TelegramClient(
        session,
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
                text = str(exc)
                log.warning("לא ניתן לפתור ערוץ · %s · %s", handle, text)
                # ניתוק הוא תקלה שלנו, לא של הערוץ. לסמן אותו על
                # המקור מטעה — הוא נראה שבור בזמן שהוא תקין לגמרי.
                if "disconnected" in text.lower() or "not connected" in text.lower():
                    return handles
                mark_fetched(source["id"], ok=False, error=text)
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
            keep, reason, item = screen(item)
            if not keep:
                log.info("נחסם  %-22s %s", reason[:22], item["title"][:50])
                return
            outcome = save(item)
            log.info("%-6s %-9s %-12s %s", outcome, item["severity"],
                     (item.get("location_name") or "—")[:12], item["title"][:60])
        except Exception as exc:
            log.error("שמירה נכשלה · %s", exc)

    async def periodic_refresh():
        while True:
            await asyncio.sleep(RESUBSCRIBE_SEC)
            if not client.is_connected():
                # החיבור נפל; ההרצה החדשה תיצור משימת רענון משלה
                log.info("רענון מדלג — הלקוח מנותק")
                return
            try:
                await refresh()
            except Exception as exc:
                log.warning("רענון מקורות נכשל · %s", exc)

    # דליפת משימות: הגרסה הקודמת יצרה משימת רענון ולא ביטלה אותה.
    # בכל ניתוק והתחברות מחדש נוצרה משימה נוספת, בעוד הישנות
    # המשיכו לרוץ מול לקוח מת — וכל אחת מהן כתבה
    # "Cannot send requests while disconnected" לשדה last_error של
    # *כל* ערוצי הטלגרם. זה מה שהמשתמש ראה באתר אחרי רבע שעה.
    async def periodic_poll():
        """סריקה יזומה של הערוצים, במקביל להאזנה החיה.

        למה גם וגם: טלגרם דוחף עדכונים בזמן אמת עבור צ'אטים
        שהחשבון חבר בהם. לגבי ערוץ ציבורי שלא הצטרפת אליו,
        קריאה יזומה (iter_messages) עובדת בוודאות, אבל דחיפה
        חיה — לא בהכרח. לא מצאתי לכך תיעוד חד־משמעי, והמחיר
        של טעות הוא פיד ששותק בלי להודיע.

        לכן הסריקה הזו רצה בכל מקרה. אם ההאזנה החיה עובדת,
        ההודעה כבר נקלטה והדה-דופליקציה תדלג עליה בזול. אם לא,
        היא נקלטת כאן תוך שלוש דקות לכל היותר.
        """
        while True:
            await asyncio.sleep(POLL_SEC)
            if not client.is_connected():
                return
            picked = 0
            for source in list(channel_map.values()):
                handle = _handle(source)
                if not handle:
                    continue
                try:
                    async for message in client.iter_messages(handle, limit=POLL_LIMIT):
                        item = _build_item(source, message)
                        if not item:
                            continue
                        keep, _reason, item = screen(item)
                        if keep and save(item) == "inserted":
                            picked += 1
                except FloodWaitError as exc:
                    log.warning("FloodWait %ss בסריקה · %s", exc.seconds, handle)
                    await asyncio.sleep(exc.seconds + 2)
                except Exception as exc:
                    log.debug("סריקה נכשלה · %s · %s", handle, exc)
                await asyncio.sleep(1.2)
            if picked:
                log.info("סריקה יזומה · %d הודעות חדשות", picked)

    refresh_task = asyncio.create_task(periodic_refresh())
    poll_task = asyncio.create_task(periodic_poll())
    try:
        await client.run_until_disconnected()
    finally:
        for task in (refresh_task, poll_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
        log.info("מאזין הטלגרם נסגר, המשימות נוקו")
