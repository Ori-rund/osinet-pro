"""
נקודת הכניסה של הוורקר.

מריץ שני דברים במקביל בתוך אותו תהליך:
  • מאזין טלגרם — חי, חוסם
  • לולאת RSS — כל RSS_INTERVAL_SEC

הרצה:
    python main.py            # שניהם
    python main.py rss        # RSS בלבד, סבב אחד ויציאה
    python main.py telegram   # טלגרם בלבד
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


async def rss_loop() -> None:
    """RSS בלולאה. שגיאה בסבב אחד לא מפילה את הוורקר."""
    import rss_source

    while True:
        try:
            await asyncio.to_thread(rss_source.run_once)
        except Exception as exc:
            log.exception("סבב RSS נכשל · %s", exc)
        await asyncio.sleep(settings.rss_interval_sec)


FILTER_RULES_REFRESH_SEC = 600  # 10 דק' — ראו filter_rules_loop


async def filter_rules_loop() -> None:
    """טוען מחדש את חוקי הסינון מה-DB כל FILTER_RULES_REFRESH_SEC.

    load_db_rules() נטען פעם אחת בלבד ב-main() לפני הלולאה הזו —
    בלי רענון תקופתי, עריכת חוק חסימה/קידום דרך ממשק הניהול לא
    נכנסת לתוקף עד לדיפלוי הבא. זה בדיוק מה שגרם לדיווח "הרס בתים"
    להמשיך לעלות שעות אחרי שהחוק שלו תוקן ישירות ב-DB.
    """
    import relevance

    while True:
        await asyncio.sleep(FILTER_RULES_REFRESH_SEC)
        try:
            relevance.load_db_rules()
        except Exception as exc:
            log.warning("רענון חוקי סינון נכשל · %s", exc)


OREF_POLL_SEC = 6  # תכוף בכוונה — זה המקור הכי מהיר וסמכותי שיש


async def oref_loop() -> None:
    """פולינג תכוף לפיקוד העורף. שגיאה בסבב אחד לא מפילה את הוורקר."""
    import oref_source

    while True:
        try:
            await asyncio.to_thread(oref_source.run_once)
        except Exception as exc:
            log.exception("סבב פיקוד העורף נכשל · %s", exc)
        await asyncio.sleep(OREF_POLL_SEC)


STALE_RESOLVE_CHECK_SEC = 900  # 15 דק' — מספיק ביחס לחלון 3 השעות עצמו


async def stale_resolve_loop() -> None:
    """סוגר אוטומטית אירועים בלי עדכון 3+ שעות. שגיאה בסבב אחד לא מפילה את הוורקר."""
    from store import auto_resolve_stale_reports

    while True:
        await asyncio.sleep(STALE_RESOLVE_CHECK_SEC)
        try:
            n = await asyncio.to_thread(auto_resolve_stale_reports)
            if n:
                log.info("נסגרו אוטומטית %d אירועים ישנים (3+ שעות בלי עדכון)", n)
        except Exception as exc:
            log.warning("סגירה אוטומטית של אירועים ישנים נכשלה · %s", exc)


STARTUP_GRACE_SEC = 25  # ראו הערה לפני הלולאה למטה


async def telegram_loop() -> None:
    """מאזין טלגרם עם חיבור מחדש. ניתוקים הם שגרה, לא תקלה."""
    import telegram_source

    # תקרת 120 שניות ולא 600: פיד חי שמחכה עשר דקות להתחברות
    # מחדש הוא פיד מת לכל דבר מעשי.
    backoff = 5
    first_attempt = True
    while True:
        try:
            if first_attempt:
                # Railway מתחיל את הקונטיינר החדש ורק *אחריו* שולח
                # SIGTERM לישן (ראה deployment-teardown docs) — כלומר
                # ברגע שהתהליך הזה עולה, הישן עדיין עלול להיות מחובר
                # באמת לאותו session. גם עם ה-SIGTERM handler ב-main()
                # שמנתק אותו בצורה מסודרת, לישן יש עד
                # RAILWAY_DEPLOYMENT_DRAINING_SECONDS (15) לסיים את
                # הניתוק. בלי חיכיון כאן, client.start() רץ מיד
                # ומתנגש בחיבור הישן שעדיין לא נסגר — בדיוק
                # AuthKeyDuplicatedError. חיכיון קצר, ארוך מזמן החסד
                # של הישן, סוגר את החלון הזה.
                first_attempt = False
                log.info("ממתין %ds לפני חיבור טלגרם — נותן לקונטיינר "
                          "הקודם (אם יש) זמן להתנתק", STARTUP_GRACE_SEC)
                await asyncio.sleep(STARTUP_GRACE_SEC)
            await telegram_source.run()
            log.warning("מאזין הטלגרם הסתיים — מתחבר מחדש")
            backoff = 5
        except Exception as exc:
            log.exception("מאזין הטלגרם קרס · %s", exc)
            backoff = min(backoff * 2, 120)
            # רשת ביטחון: אם run() קרס לפני שהגיע ל-finally שלו
            # (למשל AuthKeyDuplicatedError בתוך client.start()), לא
            # נכתב "מנותק" משם — נכתב כאן, כדי שהאתר לא יישאר תקוע
            # על "מחובר" ישן בזמן שהתהליך בפועל בלולאת ניסיונות.
            try:
                from store import set_telegram_status
                set_telegram_status(False, str(exc)[:200])
            except Exception:
                pass
        await asyncio.sleep(backoff)


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if settings.dry_run:
        log.warning("DRY_RUN פעיל — שום דבר לא נכתב ל-Supabase")

    if not settings.dry_run:
        import relevance
        relevance.load_db_rules()
        if settings.ai_filter_ready:
            await asyncio.to_thread(relevance.selftest_ai_providers)
    log.info("סינון AI: %s", "פעיל" if settings.ai_filter_ready else "כבוי — היוריסטיקה")

    if mode == "rss":
        import rss_source
        rss_source.run_once()
        return
    if mode == "telegram":
        await telegram_loop()
        return

    tasks = [asyncio.create_task(rss_loop()), asyncio.create_task(oref_loop())]
    if not settings.dry_run:
        tasks.append(asyncio.create_task(filter_rules_loop()))
        tasks.append(asyncio.create_task(stale_resolve_loop()))
    if settings.telegram_ready:
        tasks.append(asyncio.create_task(telegram_loop()))
    else:
        log.warning("טלגרם לא מוגדר — רץ עם RSS בלבד")

    # Railway (וכל מארח PaaS דומה) שולח SIGTERM לקונטיינר הישן בכל
    # דיפלוי, עם זמן חסד לכיבוי מסודר לפני SIGKILL. פייתון לא
    # מטפל ב-SIGTERM כברירת מחדל (רק ב-SIGINT/Ctrl+C) — בלי handler,
    # התהליך פשוט נהרג, telegram_source.run() לעולם לא מגיע ל-
    # finally שלו, ו-client.disconnect() לעולם לא נשלח לטלגרם.
    # החיבור הישן נשאר "חי" מבחינת השרתים של טלגרם בדיוק בחלון הזמן
    # שבו הקונטיינר החדש כבר מתחבר — זה בדיוק AuthKeyDuplicatedError.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # פלטפורמות בלי תמיכה ב-signal handlers (Windows)

    stop_task = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
    if stop.is_set():
        log.warning("התקבל אות עצירה — מנתק את טלגרם בצורה מסודרת")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("נעצר")
