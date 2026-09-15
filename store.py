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


def find_duplicate(dedup_key: str, content: str, window_hours: int,
                   location: str | None = None) -> dict | None:
    """מחפש אירוע קיים שההודעה הזו היא דיווח נוסף עליו.

    שני שלבים: איתור מועמדים, ואז הכרעה לפי דמיון טקסטואלי.

    לאיתור המועמדים משתמשים ב-location_name הממשי ולא בגיבוב שלו.
    הגרסה הקודמת השוותה dedup_key, וזה תלה את האיחוד בכך ששתי
    השורות נוצרו על ידי אותה גרסת קוד: אחרי כל שינוי באלגוריתם
    המפתח, השורות הישנות הפכו לבלתי נגישות לשורות החדשות. מכאן
    הגיע ה-0 העקשן. עמודה אמיתית לא סובלת מזה.

    dedup_key נשאר הגיבוי לדיווחים שלא זוהה בהם מיקום.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    query = db().table("reports").select("id,title,content,source_count,severity")

    if location:
        query = query.eq("location_name", location)
    else:
        # שני שלישים מהדיווחים לא מקבלים מיקום, ולאלה לא הייתה
        # שום נקודת עגינה — הם נבדקו מול מפתח טוקנים שנשבר מכל
        # שינוי ניסוח, כלומר בפועל לא נבדקו כלל.
        # במקום זה: משווים מול כל הדיווחים בחלון הזמן. החלון קטן
        # (עשרות שורות), Jaccard זול, וההכרעה ממילא נופלת על
        # מדד הדמיון ולא על המפתח.
        pass

    result = (query.gte("published_at", since)
              .order("published_at", desc=True).limit(MAX_CANDIDATES).execute())

    best, best_score = None, 0.0
    for candidate in result.data or []:
        score = similarity(content, candidate.get("content") or "")
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= SIMILARITY_THRESHOLD else None


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def attach_source(report: dict, item: dict, *, append_note: bool = False) -> None:
    """דיווח חוזר על אירוע קיים — מוסיפים אסמכתא, לא כרטיס חדש.

    אם המקור החדש מדווח על חומרה גבוהה יותר, האירוע מתעדכן כלפי מעלה.
    שמונה ערוצים שמדווחים על אותו דבר זה אות אמינות, לא רעש.

    published_at מתעדכן לעכשיו בכל אישוש. זה גם מה שמראה למשתמש
    שהדיווח נערך זה עתה, וגם מה ש"מחזיר לחיים" אירוע שכבר עמד לצאת
    מחלון התצוגה (ראה get_reports_for_user) — אירוע שעדיין מדווח
    עליו בפועל נשאר טרי, אירוע ששכח ממנו העולם פשוט נעלם בשקט.

    append_note מיועד לפרגמנטים קצרים כמו "ללא נפגעים" (ראה
    _is_followup_fragment) — במקום כרטיס עצמאי חסר הקשר, הטקסט
    מתווסף לדיווח הקיים כעדכון.
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

    patch: dict = {
        "source_count": (report.get("source_count") or 1) + 1,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    old = _SEVERITY_RANK.get(report.get("severity") or "low", 0)
    new = _SEVERITY_RANK.get(item.get("severity") or "low", 0)
    if new > old:
        patch["severity"] = item["severity"]

    if append_note:
        note = (item.get("content") or "").strip()
        base = (report.get("content") or "").strip()
        if note and note not in base:
            patch["content"] = f"{base} · עדכון: {note}" if base else note

    # "התברר שווא"/"חזרה לשגרה"/"לא נמצא ממצא" — האירוע נסגר. נבדק
    # על תוכן המקור החדש תמיד, לא רק בזרימת ה-append_note, כי דיווח
    # סגירה לרוב מגיע כפסקה מלאה שמאוחדת דרך דמיון טקסטואלי רגיל
    # (find_duplicate) ולא כפרגמנט קצר. חזרה לשגרה במקום דיווח תקוע
    # כ"פעיל" לנצח, ויורד מהמפה (index.html מסנן dismissed משם) בלי
    # להיעלם מרשימת הדיווחים.
    new_text = (item.get("content") or "").strip() or (item.get("title") or "").strip()
    if new_text and _is_resolved(new_text):
        patch["status"] = "dismissed"

    db().table("reports").update(patch).eq("id", report["id"]).execute()


# פרגמנטים קצרים שממשיכים אירוע קיים ולא פותחים אחד חדש — "ללא
# נפגעים" אחרי דיווח על תקיפה, "עודכן" וכו'. הם קצרים מדי בשביל
# שמדד הדמיון הרגיל יתפוס אותם כהמשך לדיווח המקורי (אין ביניהם
# חפיפת מילים), אבל מההקשר ברור שזה בדיוק מה שהם.
_FOLLOWUP_MAX_CHARS = 60
_FOLLOWUP_MARKERS = [
    "נפגעים", "עודכן", "עדכון:", "חזר לשגרה", "בוטלה ההתרעה",
    "האירוע הסתיים", "המצב רגוע", "פונה נפגע",
]

# תת-קבוצה של הפרגמנטים שאומרת "האירוע נסגר, זו לא הייתה תקיפה" —
# בניגוד ל"ללא נפגעים" (האירוע קרה אבל בלי פגיעה), אלה אומרים
# שלא היה כלום מלכתחילה. גם משנה סטטוס, לא רק מוסיף עדכון.
_RESOLVED_MARKERS = [
    "חזר לשגרה", "חזרה לשגרה", "אזעקת שווא", "כוזב", "כוזבת",
    "בוטלה ההתרעה", "ללא ממצא", "ללא ממצאים", "לא נמצא דבר",
    "לא נמצאו ממצאים", "לא נמצא כל ממצא", "לא אותרו ממצאים",
    "אין חשד לפעילות עוינת", "התברר כי לא", "התברר שמדובר בכוזב",
]


def _is_followup_fragment(content: str) -> bool:
    text = (content or "").strip()
    if not text or len(text) > _FOLLOWUP_MAX_CHARS:
        return False
    return any(marker in text for marker in _FOLLOWUP_MARKERS + _RESOLVED_MARKERS)


def _is_resolved(content: str) -> bool:
    text = (content or "").strip()
    return any(marker in text for marker in _RESOLVED_MARKERS)


def find_latest_report(window_minutes: int = 15) -> dict | None:
    """הדיווח האחרון שנכתב — יעד לצירוף פרגמנט קצר (ראה למעלה).

    לא מבוסס דמיון טקסטואלי בכוונה. חלון קצר (15 דקות, לא 45) —
    אותו ערוץ שמפרסם "ללא נפגעים" גם בבוקר וגם בצהריים מדווח על שני
    אירועים שונים, לא מאשש פעמיים את אותו אחד. זיהוי אמיתי של "האם
    זה אותו אירוע" דורש הבנת הקשר (מיקום, זמן, תוכן) שהתאמת מחרוזת
    לא נותנת — זו בדיוק המגבלה של גישה היוריסטית טהורה.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    result = (
        db().table("reports").select("id,title,content,source_count,severity")
        .gte("created_at", since).order("created_at", desc=True).limit(1).execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


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

    if _is_followup_fragment(item.get("content")):
        latest = find_latest_report()
        if latest:
            attach_source(latest, item, append_note=True)
            return "deduped"
        # אין דיווח קרוב לצרף אליו — פרגמנט כמו "ללא נפגעים" לבד
        # הוא כרטיס חסר משמעות (בדיוק המקרה שקרה בבאקפיל: הפרגמנט
        # נקלט לפני האירוע שהוא ממשיך, אז אין "אחרון" לצרף אליו).
        # עדיף לדלג מאשר לפרסם כרטיס עצמאי ריק מתוכן.
        return "skipped"

    existing = find_duplicate(
        item.get("dedup_key") or "", item.get("content") or "",
        settings.dedup_window_hours, item.get("location_name"),
    )
    if existing:
        attach_source(existing, item)
        return "deduped"

    return "inserted" if insert_report(item) else "skipped"
