"""
קליטת RSS מאתרי חדשות.

הערה משפטית שחשובה יותר ממה שהיא נשמעת: אנחנו שומרים כותרת, תקציר
וקישור — לא את גוף הכתבה. זה השימוש המקובל בפיד RSS. העתקת כתבות
מלאות לכלי ארגוני היא חשיפה מיותרת, ולכן MAX_SUMMARY_CHARS קיים.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import feedparser
import httpx

from config import settings
from enrich import clean_text, enrich
from relevance import screen
from store import active_sources, log_run, mark_fetched, save

log = logging.getLogger("rss")

MAX_SUMMARY_CHARS = 400
USER_AGENT = "osinet-pro/1.0 (+https://osinet-pro.vercel.app)"
_HTML_TAG = re.compile(r"<[^>]+>")


def _published(entry) -> str:
    """זמן הפרסום המקורי. נופל להווה אם הפיד לא מספק — ואז הסדר בפיד משתבש."""
    for field in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc).isoformat()
            except (ValueError, OverflowError):
                pass
    return datetime.now(timezone.utc).isoformat()


def _summary(entry) -> str:
    for field in ("summary", "description"):
        value = getattr(entry, field, "") or ""
        if value:
            # feedparser מחזיר HTML; clean_text לא מסיר תגיות, אז נסיר כאן
            return clean_text(_HTML_TAG.sub(" ", value))[:MAX_SUMMARY_CHARS]
    content = getattr(entry, "content", None)
    if content:
        return clean_text(_HTML_TAG.sub(" ", content[0].get("value", "")))[:MAX_SUMMARY_CHARS]
    return ""


def fetch_feed(source: dict) -> dict:
    """מושך פיד אחד. מחזיר ספירות; לא זורק — מקור שנפל לא מפיל את הריצה."""
    started = time.monotonic()
    url = source.get("handle") or source.get("url")
    counts = {"fetched": 0, "inserted": 0, "deduped": 0, "skipped": 0, "filtered": 0}

    try:
        # feedparser מושך בעצמו, אבל httpx נותן לנו timeout ו-UA אמיתיים
        response = httpx.get(
            url, timeout=20.0, follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception as exc:
        log.warning("פיד נכשל · %s · %s", source.get("name"), exc)
        mark_fetched(source["id"], ok=False, error=str(exc))
        log_run(source["id"], ok=False, error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000))
        return counts

    entries = feed.entries[: settings.max_items_per_fetch]
    counts["fetched"] = len(entries)

    for entry in entries:
        title = clean_text(getattr(entry, "title", "") or "")
        summary = _summary(entry)
        link = getattr(entry, "link", "") or ""
        # guid עדיף על link — יש אתרים שמשנים URL אחרי פרסום
        external_id = getattr(entry, "id", None) or link or title
        if not title and not summary:
            continue

        published = _published(entry)
        body = f"{title}. {summary}".strip()
        enriched = enrich(
            body,
            default_severity=source.get("default_severity") or "low",
            when=datetime.fromisoformat(published),
        )
        # לכותרת RSS יש ערך — לא מחליפים אותה בכותרת שנגזרה מהטקסט
        enriched["title"] = title or enriched["title"]
        enriched["content"] = summary or title

        candidate = enriched | {
            "source_id": source["id"],
            "source_name": source.get("name"),
            "source_type": "rss",
            "source_url": link,
            "external_id": external_id,
            "published_at": published,
            "raw": {"feed": url, "guid": str(external_id)},
        }

        # הסינון קורה לפני הכתיבה. דיווח שנחסם לא נוגע ב-DB בכלל.
        keep, reason, candidate = screen(candidate)
        if not keep:
            counts["filtered"] = counts.get("filtered", 0) + 1
            log.debug("נחסם · %s · %s", reason, candidate["title"][:50])
            continue

        outcome = save(candidate)
        counts[outcome] = counts.get(outcome, 0) + 1

    mark_fetched(source["id"], ok=True)
    log_run(source["id"], ok=True, fetched=counts["fetched"],
            inserted=counts["inserted"], deduped=counts["deduped"],
            duration_ms=int((time.monotonic() - started) * 1000))
    log.info("%-22s נמשכו %2d · חדשים %2d · כפולים %2d · סוננו %2d",
             (source.get("name") or "")[:22], counts["fetched"],
             counts["inserted"], counts["deduped"], counts["filtered"])
    return counts


def run_once() -> dict:
    """סבב אחד על כל מקורות ה-RSS הפעילים."""
    sources = [s for s in active_sources()
               if (s.get("source_type") or "").lower() in ("rss", "web", "news")]
    if not sources:
        log.warning("אין מקורות RSS פעילים בטבלת sources")
        return {}

    totals = {"fetched": 0, "inserted": 0, "deduped": 0, "skipped": 0, "filtered": 0}
    for source in sources:
        for key, value in fetch_feed(source).items():
            totals[key] = totals.get(key, 0) + value
    log.info("סבב RSS הסתיים · %s", totals)
    return totals
