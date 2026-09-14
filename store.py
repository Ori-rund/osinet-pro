"""
גישה ל-Supabase — כל הכתיבות עוברות דרך כאן.

הסיבה שזה קובץ נפרד: כל לוגיקת הדה-דופליקציה יושבת בנקודה אחת.
אם תוסיף מקור חדש מחר, הוא מקבל את אותה הגנה מפני כפילויות בחינם.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from supabase import Client, create_client

from config import settings
from enrich import similarity

log = logging.getLogger("store")

_client: Client | None = None


def db() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_service_key)
    return _client


# ─────────────────────────────────────────────────────────────
# מקורות
# ─────────────────────────────────────────────────────────────
def active_sources(source_type: str | None = None) -> list[dict]:
    """המקורות הפעילים. כיבוי מקור בממשק האתר עוצר אותו כאן מיד."""
    query = (
        db().table("sources")
        .select("id,name,url,handle,source_type,is_active,priority,"
                "default_severity,trust_score,fetch_interval_sec,last_fetched_at")
        .eq("is_active", True)
    )
    if source_type:
        query = query.eq("source_type", source_type)
    return query.order("priority", desc=True).execute().data or []


def mark_fetched(source_id: str, *, ok: bool, error: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    patch: dict = {"last_fetched_at": now}
    if ok:
        patch |= {"last_success_at": now, "last_error": None, "error_count": 0}
    else:
        patch["last_error"] = (error or "")[:500]
    db().table("sources").update(patch).eq("id", source_id).execute()


def log_run(source_id: str, *, ok: bool, fetched: int = 0, inserted: int = 0,
            deduped: int = 0, duration_ms: int | None = None,
            error: str | None = None) -> None:
    """ingest_log הוא איך תדע שמקור מת בשקט לפני שבוע."""
    try:
        db().table("ingest_log").insert({
            "source_id": source_id, "ok": ok, "fetched": fetched,
            "inserted": inserted, "deduped": deduped,
            "duration_ms": duration_ms, "error": (error or None),
        }).execute()
    except Exception as exc:  # לוג שנכשל לא יפיל איסוף
        log.warning("ingest_log failed: %s", exc)


# ─────────────────────────────────────────────────────────────
# דיווחים
# ─────────────────────────────────────────────────────────────
def already_ingested(source_id: str, external_id: str) -> bool:
    """בדיקה מקדימה — חוסכת עבודה. האינדקס הייחודי הוא הבלם האמיתי."""
    result = (
        db().table("reports").select("id")
        .eq("source_id", source_id).eq("external_id", str(external_id))
        .limit(1).execute()
    )
    return bool(result.data)


# סף הדמיון לאיחוד שני דיווחים. כוונן על טקסטים עבריים אמיתיים:
# 0.50 לאותו אירוע בניסוח שונה מול 0.15 לשני אירועים באותו יישוב.
# העלאה מעל 0.5 תחמיץ איחודים; הורדה מתחת ל-0.25 תאחד אירועים שונים.
# הורד מ-0.35 אחרי שהנתונים האמיתיים הראו 0 איחודים מתוך 121
# דיווחים. אתרי חדשות מנסחים כותרות שונה מאוד זה מזה, ו-0.30
# עדיין משאיר מרווח נוח מול ~0.15 שמקבלים אירועים שונים.
SIMILARITY_THRESHOLD = 0.30
MAX_CANDIDATES = 40


def find_duplicate(dedup_key: str, content: str, window_hours: int) -> dict | None:
    """מחפש אירוע קיים שההודעה הזו היא דיווח נוסף עליו.

    שני שלבים: dedup_key מצמצם למועמדים (אותו מקום, אותו חלון זמן),
    ו-similarity מכריע. המפתח לבדו גס מדי — שני אירועים שונים
    בקריית שמונה באותה שעה חולקים אותו מפתח.
    """
    if not dedup_key:
        return None
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    result = (
        db().table("reports")
        .select("id,title,content,source_count,severity")
        .eq("dedup_key", dedup_key).gte("published_at", since)
        .order("published_at", desc=True).limit(MAX_CANDIDATES).execute()
    )
    best, best_score = None, 0.0
    for candidate in result.data or []:
        score = similarity(content, candidate.get("content") or "")
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= SIMILARITY_THRESHOLD else None


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def attach_source(report: dict, item: dict) -> None:
    """דיווח חוזר על אירוע קיים — מוסיפים אסמכתא, לא כרטיס חדש.

    אם המקור החדש מדווח על חומרה גבוהה יותר, האירוע מתעדכן כלפי מעלה.
    שמונה ערוצים שמדווחים על אותו דבר זה אות אמינות, לא רעש.
    """
    db().table("report_sources").upsert({
        "report_id": report["id"],
        "source_id": item.get("source_id"),
        "source_name": item.get("source_name"),
        "source_url": item.get("source_url"),
        "external_id": str(item.get("external_id") or ""),
        "published_at": item.get("published_at"),
        "excerpt": (item.get("content") or "")[:280],
    }, on_conflict="report_id,source_id,external_id").execute()

    patch: dict = {"source_count": (report.get("source_count") or 1) + 1}
    old = _SEVERITY_RANK.get(report.get("severity") or "low", 0)
    new = _SEVERITY_RANK.get(item.get("severity") or "low", 0)
    if new > old:
        patch["severity"] = item["severity"]

    db().table("reports").update(patch).eq("id", report["id"]).execute()


def insert_report(item: dict) -> str | None:
    """מוסיף דיווח חדש. מחזיר id, או None אם נדחה ככפילות."""
    payload = {
        "title": item["title"],
        "content": item["content"],
        "severity": item.get("severity", "low"),
        "status": item.get("status", "verified"),
        "source_id": item.get("source_id"),
        "source_name": item.get("source_name"),
        "source_type": item.get("source_type"),
        "source_url": item.get("source_url"),
        "external_id": str(item["external_id"]) if item.get("external_id") else None,
        "dedup_key": item.get("dedup_key"),
        "location_name": item.get("location_name"),
        "latitude": item.get("latitude"),
        "longitude": item.get("longitude"),
        "published_at": item.get("published_at"),
        "lang": item.get("lang"),
        "raw": item.get("raw"),
    }
    try:
        result = db().table("reports").insert(payload).execute()
    except Exception as exc:
        # 23505 = הפרת האינדקס הייחודי. הודעה שכבר נקלטה — זה תקין.
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            log.debug("duplicate skipped: %s", item.get("external_id"))
            return None
        raise

    if not result.data:
        return None

    report_id = result.data[0]["id"]
    # גם המקור הראשון נרשם, כדי ש-report_sources יהיה תמונה מלאה
    db().table("report_sources").upsert({
        "report_id": report_id,
        "source_id": item.get("source_id"),
        "source_name": item.get("source_name"),
        "source_url": item.get("source_url"),
        "external_id": str(item.get("external_id") or ""),
        "published_at": item.get("published_at"),
        "excerpt": (item.get("content") or "")[:280],
    }, on_conflict="report_id,source_id,external_id").execute()
    return report_id


def save(item: dict) -> str:
    """נקודת הכניסה היחידה. מחזיר 'inserted' | 'deduped' | 'skipped'."""
    if settings.dry_run:
        loc = item.get("location_name") or "—"
        log.info("[DRY] %-8s %-14s %s", item.get("severity"), loc, item.get("title", "")[:70])
        return "inserted"

    if item.get("source_id") and item.get("external_id"):
        if already_ingested(item["source_id"], str(item["external_id"])):
            return "skipped"

    existing = find_duplicate(
        item.get("dedup_key") or "", item.get("content") or "",
        settings.dedup_window_hours,
    )
    if existing:
        attach_source(existing, item)
        return "deduped"

    return "inserted" if insert_report(item) else "skipped"
