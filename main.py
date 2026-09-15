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


async def telegram_loop() -> None:
    """מאזין טלגרם עם חיבור מחדש. ניתוקים הם שגרה, לא תקלה."""
    import telegram_source

    # תקרת 120 שניות ולא 600: פיד חי שמחכה עשר דקות להתחברות
    # מחדש הוא פיד מת לכל דבר מעשי.
    backoff = 5
    while True:
        try:
            await telegram_source.run()
            log.warning("מאזין הטלגרם הסתיים — מתחבר מחדש")
            backoff = 5
        except Exception as exc:
            log.exception("מאזין הטלגרם קרס · %s", exc)
            backoff = min(backoff * 2, 120)
        await asyncio.sleep(backoff)


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if settings.dry_run:
        log.warning("DRY_RUN פעיל — שום דבר לא נכתב ל-Supabase")

    if not settings.dry_run:
        import relevance
        relevance.load_db_rules()
    log.info("סינון AI: %s", "פעיל" if settings.ai_filter_ready else "כבוי — היוריסטיקה")

    if mode == "rss":
        import rss_source
        rss_source.run_once()
        return
    if mode == "telegram":
        await telegram_loop()
        return

    tasks = [asyncio.create_task(rss_loop())]
    if settings.telegram_ready:
        tasks.append(asyncio.create_task(telegram_loop()))
    else:
        log.warning("טלגרם לא מוגדר — רץ עם RSS בלבד")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("נעצר")
