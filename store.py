"""
גישה ל-Supabase — כל הכתיבות עוברות דרך כאן.

הסיבה שזה קובץ נפרד: כל לוגיקת הדה-דופליקציה יושבת בנקודה אחת.
אם תוסיף מקור חדש מחר, הוא מקבל את אותה הגנה מפני כפילויות בחינם.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from supabase import Client, create_client

from config import settings
from enrich import significant_overlap, similarity

log = logging.getLogger("store")

_client: Client | None = None


def db() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_service_key)
    return _client


MEDIA_BUCKET = "report-media"


def upload_media(data: bytes, path: str, content_type: str) -> str | None:
    """מעלה תמונה/סרטון ל-Storage הציבורי, מחזיר URL או None בכל כשל.

    כשל בהעלאה לא אמור לחסום את קליטת הדיווח עצמו — דיווח טקסטואלי
    בלי מדיה עדיף על שום דיווח.
    """
    try:
        db().storage.from_(MEDIA_BUCKET).upload(
            path, data, {"content-type": content_type, "upsert": "true"},
        )
        return db().storage.from_(MEDIA_BUCKET).get_public_url(path)
    except Exception as exc:
        log.warning("העלאת מדיה נכשלה · %s · %s", path, exc)
        return None


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


def set_telegram_status(connected: bool, detail: str = "") -> None:
    """שורה אחת ב-system_status שהאתר קורא בזמן אמת (realtime, לא
    polling) כדי להראות "מחובר לטלגרם" בלי לטעון את השרת. נקראת
    בהתחברות, בניתוק, ובלב פועם תקופתי כדי שתהליך תקוע (לא קרס,
    פשוט נתקע) גם יתגלה — updated_at ישן מדי נחשב "מנותק" בצד הלקוח.
    """
    try:
        db().table("system_status").upsert({
            "key": "telegram", "connected": connected,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "detail": (detail or "")[:200],
        }).execute()
    except Exception as exc:  # סטטוס שנכשל לא יפיל את האיסוף
        log.warning("set_telegram_status failed: %s", exc)


def get_ai_status(provider: str) -> dict | None:
    """הסטטוס האחרון שנשמר לספק AI, או None אם לא ידוע/הקריאה נכשלה."""
    try:
        rows = (db().table("system_status").select("connected,updated_at,detail")
                .eq("key", f"ai_{provider}").limit(1).execute().data)
        return rows[0] if rows else None
    except Exception as exc:
        log.debug("get_ai_status נכשל: %s", exc)
        return None


def set_ai_status(provider: str, available: bool, detail: str = "") -> None:
    """אותו מנגנון בדיוק כמו set_telegram_status, לכל ספק AI בנפרד.

    שורה נפרדת per-provider (key='ai_groq'/'ai_gemini') ולא שורה
    אחת משותפת — כדי שאם גם Groq וגם Gemini מחוברים, האתר יציג את
    שניהם, לא רק את מי שענה בפועל לקריאה האחרונה (שרשרת ה-fallback
    ב-_call_ai עוצרת אצל הראשון שמצליח, ולכן ספק שני שמצליח בפועל
    כל פעם שנקרא אליו לא אמור להיראות "לא ידוע" סתם כי לא היה
    צריך אותו הפעם).
    """
    try:
        db().table("system_status").upsert({
            "key": f"ai_{provider}", "connected": available,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "detail": (detail or "")[:200],
        }).execute()
    except Exception as exc:
        log.warning("set_ai_status failed: %s", exc)


# ─────────────────────────────────────────────────────────────
# דיווחים
# ─────────────────────────────────────────────────────────────
def already_ingested(source_id: str, external_id: str) -> bool:
    """בדיקה מקדימה — חוסכת עבודה. האינדקס הייחודי הוא הבלם האמיתי.

    בודק קודם seen_sources — לוג קבוע, בלתי תלוי בקיום reports/
    report_sources (ראה mark_seen). בלעדיו: מנהל שמוחק דיווח מוחק
    בקסקייד גם את report_sources שלו, ובאקפיל הבא שסורק את אותו
    ערוץ רואה הודעה "חדשה" ומכניס אותה בחזרה — בדיוק ה"מחיקה שלא
    מחזיקה מעמד" שדווחה בפועל.

    נופל גם ל-reports/report_sources כגיבוי, למקרה שההודעה נקלטה
    לפני שהטבלה הזו נוספה. באג קודם ודומה (לפני seen_sources): הודעה
    שצורפה כמקור נוסף לא הופיעה בעמודות source_id/external_id של
    reports (אלה שייכות למקור הראשי בלבד) — ובאקפיל שסרק אותה שוב
    לא זיהה שהיא כבר טופלה, בנה שרשרת איחוד כפולה על אירוע פשיטה
    ביטא. הבדיקה השנייה כאן היא התיקון לזה, ונשארת כרשת ביטחון.
    """
    seen = (
        db().table("seen_sources").select("source_id")
        .eq("source_id", source_id).eq("external_id", str(external_id))
        .limit(1).execute()
    )
    if seen.data:
        return True
    in_reports = (
        db().table("reports").select("id")
        .eq("source_id", source_id).eq("external_id", str(external_id))
        .limit(1).execute()
    )
    if in_reports.data:
        return True
    in_sources = (
        db().table("report_sources").select("id")
        .eq("source_id", source_id).eq("external_id", str(external_id))
        .limit(1).execute()
    )
    return bool(in_sources.data)


def mark_seen(source_id: str, external_id: str) -> None:
    """רושם ב-seen_sources שההודעה הזו עובדה — לצמיתות, גם אם הדיווח שנוצר ממנה יימחק אחר כך."""
    try:
        db().table("seen_sources").upsert(
            {"source_id": source_id, "external_id": str(external_id)},
            on_conflict="source_id,external_id",
        ).execute()
    except Exception as exc:
        log.warning("mark_seen נכשל: %s", exc)


# סף הדמיון לאיחוד שני דיווחים. כוונן על טקסטים עבריים אמיתיים:
# 0.50 לאותו אירוע בניסוח שונה מול 0.15 לשני אירועים באותו יישוב.
# העלאה מעל 0.5 תחמיץ איחודים; הורדה מתחת ל-0.25 תאחד אירועים שונים.
# הורד מ-0.35 אחרי שהנתונים האמיתיים הראו 0 איחודים מתוך 121
# דיווחים. אתרי חדשות מנסחים כותרות שונה מאוד זה מזה, ו-0.30
# עדיין משאיר מרווח נוח מול ~0.15 שמקבלים אירועים שונים.
SIMILARITY_THRESHOLD = 0.30
MAX_CANDIDATES = 40

# ערוצי טלגרם "מבזקים" מנסחים בסגנון סנסציוני ומשתנה בהרבה יותר
# מאתרי חדשות — שלושה ערוצים על אותה פשיטה ביטא נתנו ציון דמיון
# Jaccard של 0.116 בפועל, נמוך יותר משני אירועים שונים באותו יישוב
# (~0.18-0.22, נמדד ב-test_enrich). ציון גולמי לא מבדיל כאן.
#
# מה שכן מבדיל: חפיפת מילים משמעותיות מעבר לשם המיקום עצמו (ראה
# enrich.significant_overlap). שני אירועים שונים באותו יישוב חולקים
# רק את שם היישוב; אותו אירוע, בכל ניסוח, חולק גם מילת תוכן אחת
# לפחות. כשגם המיקום זהה וגם הפרסום קרוב בזמן (חלון צר בהרבה מחלון
# הדה-דופ הכללי של 6 שעות) מספיקה חפיפה כזו כדי לאחד, גם אם ה-
# Jaccard הכולל נמוך בגלל ניסוח שונה.
TIGHT_WINDOW_MINUTES = 90

# חלון צר בהרבה בשביל שאלת AI — "האם זה המשך של דיווח קיים" — כי
# בניגוד לבדיקת המילים המשותפות, זו קריאת רשת אמיתית שעולה כסף/
# מכסה. נשאלת רק כשיש בכלל מועמד קרוב בזמן, לא על כל דיווח חדש.
AI_MERGE_WINDOW_MINUTES = 20
AI_MERGE_MAX_CANDIDATES = 6


def find_duplicate(dedup_key: str, content: str, window_hours: int,
                   location: str | None = None,
                   published_at: str | None = None) -> dict | None:
    """מחפש אירוע קיים שההודעה הזו היא דיווח נוסף עליו.

    משווים דמיון טקסטואלי מול כל הדיווחים בחלון הזמן (לא רק אלה
    עם אותו location_name — ראה למטה למה), עם מסלול מקל למועמד
    שגם המיקום שלו זהה וגם הפרסום קרוב בזמן (ראה TIGHT_WINDOW_MINUTES
    למעלה).

    dedup_key לא משמש כאן לאיתור מועמדים (הגרסה הקודמת השוותה אותו,
    וזה תלה את האיחוד בכך ששתי השורות נוצרו על ידי אותה גרסת קוד —
    אחרי כל שינוי באלגוריתם המפתח, שורות ישנות הפכו לבלתי נגישות
    לחדשות). הוא עדיין נשמר על השורה כגיבוי לתצוגה בלבד.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    # לא מסננים לפי location_name ב-SQL, גם כשיש מיקום: אותו אירוע
    # ממש קיבל בפועל "יטא" בדיווח אחד ו"חברון" בדיווח אחר על אותה
    # פשיטה (extract_location בוחר את ההתאמה הראשונה בטקסט הגולמי,
    # וזה יכול להשתנות בין ניסוחים) — סינון לפי מיקום מדויק החמיץ
    # מועמד שדמיון הטקסט שלו מול הקיים עמד על 0.34, מעל הסף הרגיל.
    # החלון קטן (עשרות שורות), Jaccard זול, וההכרעה ממילא נופלת על
    # מדד הדמיון ולא על שאילתת ה-SQL.
    result = (
        db().table("reports").select("id,title,content,source_count,severity,published_at,occurred_at,location_name,raw")
        .gte("published_at", since).order("published_at", desc=True).limit(MAX_CANDIDATES).execute()
    )

    new_time = _parse_time(published_at)

    best, best_score = None, 0.0
    for candidate in result.data or []:
        candidate_content = candidate.get("content") or ""
        candidate_location = candidate.get("location_name")
        score = similarity(content, candidate_content)
        eligible = score >= SIMILARITY_THRESHOLD
        # "בלי סתירה" — לא "זהה" — כי דיווח בלי מיקום כלל (extract_location
        # לא תפס כלום, למשל "אזעקות בקו העימות" — "קו העימות" הוא לא
        # שם מקום בגזטיר) לא אמור לחסום איחוד עם דיווח שכן קיבל מיקום
        # ("במנרה ובמרגליות") על אותו אירוע ממש. סתירה אמיתית — שני
        # מיקומים שונים ומפורשים — עדיין חוסמת.
        no_location_conflict = not location or not candidate_location or location == candidate_location
        if not eligible and no_location_conflict and new_time:
            candidate_time = _parse_time(candidate.get("published_at"))
            if candidate_time and abs((new_time - candidate_time).total_seconds()) <= TIGHT_WINDOW_MINUTES * 60:
                overlap = significant_overlap(content, candidate_content, location or candidate_location or "")
                # מקרה אמיתי שדלף: כתבת ניתוח ארוכה על איראן ("תמונות
                # לווין... טלקאן 2... פרצין") בלי שום מיקום ישראלי
                # התאחדה עם עדכון לגמרי לא קשור על אירוע דריסה בחדרה —
                # שתי הכתבות ארוכות דיין שמילה משמעותית אחת (למשל
                # "פעילות") חופפת במקרה, בלי שום קשר עניינו. כשאין
                # שום עוגן מיקום בכלל בשני הצדדים (המקרה החלש ביותר —
                # ראה ההערה למעלה, שם לפחות צד אחד תרם "מנרה ומרגליות"
                # אמיתי), מילה אחת לא מספיקה; דורשים שתיים לפחות.
                min_overlap = 1 if (location or candidate_location) else 2
                eligible = len(overlap) >= min_overlap
        if eligible and score > best_score:
            best, best_score = candidate, score

    # שכבה אחרונה: ההיוריסטיקה לא מצאה שום חפיפת מילים, אבל יש
    # מועמדים ממש קרובים בזמן — "אזעקה" ואז "נראה כמו נפילה בשטח
    # פתוח" הם בדיוק המקרה הזה (ראה relevance.judge_same_event).
    # נקרא רק כשיש לפחות מועמד אחד כזה, לא על כל דיווח חדש.
    if best is None and new_time:
        near = []
        for candidate in result.data or []:
            candidate_time = _parse_time(candidate.get("published_at"))
            if candidate_time and abs((new_time - candidate_time).total_seconds()) <= AI_MERGE_WINDOW_MINUTES * 60:
                near.append(candidate)
        if near:
            try:
                import relevance
                idx = relevance.judge_same_event(content, near[:AI_MERGE_MAX_CANDIDATES])
                if idx is not None:
                    candidate = near[idx]
                    candidate_location = candidate.get("location_name")
                    candidate_content = candidate.get("content") or ""
                    same_location = bool(location) and bool(candidate_location) and location == candidate_location
                    has_overlap = bool(significant_overlap(content, candidate_content))
                    # דלף בפועל: הודעת כוננות כללית ("צה"ל מתגבר כוחות
                    # לקראת יום כיפור", בלי מיקום) התאחדה עם דיווח ממוקם
                    # וקונקרטי לגמרי (פיצוץ מוצב במרחב החרמון) — ה-AI
                    # טעה למרות שהפרומפט שלו אוסר בדיוק את זה במפורש.
                    # כשצד אחד יש לו מיקום והשני בכלל לא, ואין אף מילה
                    # משותפת, זה בדיוק התבנית שנכשלה — לא סומכים על ה-AI
                    # לבד שם, גם אם הוא "בטוח".
                    if same_location or has_overlap or (not location and not candidate_location):
                        best = candidate
                    else:
                        log.info("איחוד AI נדחה · אין מיקום משותף ואין חפיפת מילים: %s", candidate.get("id"))
            except Exception as exc:
                log.debug("בדיקת איחוד AI נכשלה: %s", exc)

    return best


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

_IL_TZ = ZoneInfo("Asia/Jerusalem")


def _il_time(value: str | None) -> str | None:
    """HH:MM:SS לפי שעון ישראל, לשורות "עדכון (מקור, שעה):" באירועים עם הרבה מקורות."""
    dt = _parse_time(value)
    return dt.astimezone(_IL_TZ).strftime("%H:%M:%S") if dt else None


_UPDATE_LINE_PREFIX = re.compile(r"^עדכון \([^)]*\):\s*")


def _is_near_duplicate_line(note: str, base: str) -> bool:
    """כמו similarity(note, base) >= SIMILARITY_THRESHOLD, אבל שורה מול
    שורה, לא מול כל ה-base כמחרוזת אחת.

    דלף בפועל באירוע נוה צוף (69 מקורות, 14,939 תווים): אותה הודעת
    דוברות מד"א המילה במילה הופיעה שלוש פעמים, ואותו ציטוט של ראש
    מועצת בנימין פעמיים — near_duplicate מול ה-base השלם לא תפס אף
    אחת מהן. נמדד בפועל: similarity(note, base-כולו) = 0.09 (base
    ארוך מדי, כל שאר המילים מדללות את היחס) מול similarity(note,
    השורה התואמת בלבד) = 1.0. משווים כל שורה קיימת בנפרד במקום, כדי
    שהיחס לא יידלל ככל שהאירוע צובר עוד ועוד מקורות.
    """
    for line in base.split("\n"):
        line = _UPDATE_LINE_PREFIX.sub("", line).strip()
        if line and similarity(note, line) >= SIMILARITY_THRESHOLD:
            return True
    return False


def attach_source(report: dict, item: dict, *, append_note: bool = False) -> None:
    """דיווח חוזר על אירוע קיים — מוסיפים אסמכתא, לא כרטיס חדש.

    אם המקור החדש מדווח על חומרה גבוהה יותר, האירוע מתעדכן כלפי מעלה.
    שמונה ערוצים שמדווחים על אותו דבר זה אות אמינות, לא רעש.

    published_at מתעדכן לעכשיו בכל אישוש — זה מה ש"מחזיר לחיים" אירוע
    שכבר עמד לצאת מחלון 24 השעות ב-get_reports_for_user, ומה שמניע
    את דירוג הטריות בפיד.

    occurred_at ו"המקור" (source_name/source_id/source_url/external_id)
    המוצגים על הכרטיס כן יכולים לזוז, אבל רק אחורה בזמן: אם המקור
    שמצטרף עכשיו פרסם *לפני* מי שכרגע רשום כ"ראשון", הוא מחליף אותו.
    למה זה קורה בפועל: מקורות שונים לא נקלטים תמיד בסדר שבו הם
    התפרסמו (טלגרם מגיע דרך polling, ערוצים שונים נסרקים בקצב שונה)
    — ה-report שנוצר ראשון בבסיס הנתונים שלנו הוא לא בהכרח מי שדיווח
    ראשון במציאות. "מי שהביא את הדיווח הכי מוקדם — זכה" בתצוגה,
    תמיד, גם אם זה לא מי שיצר את השורה.

    append_note מיועד לפרגמנטים קצרים כמו "ללא נפגעים" (ראה
    _is_followup_fragment) — במקום כרטיס עצמאי חסר הקשר, הטקסט
    מתווסף לדיווח הקיים כעדכון.
    """
    source_id = item.get("source_id")
    external_id = str(item.get("external_id") or "")
    # נבדק *לפני* ה-upsert: אם המקור הזה כבר מצורף (למשל אותה הודעה
    # שנסרקה שוב בבאקפיל אחרי redeploy), ה-upsert רק מרענן את הרשומה
    # הקיימת ולא מוסיף מקור אמיתי — source_count לא אמור לעלות על
    # כל אישוש חוזר של אותו מקור, רק כשמצטרף מקור חדש בפועל.
    already_attached = bool(
        db().table("report_sources").select("id")
        .eq("report_id", report["id"]).eq("source_id", source_id).eq("external_id", external_id)
        .limit(1).execute().data
    )

    db().table("report_sources").upsert({
        "report_id": report["id"],
        "source_id": source_id,
        "source_name": item.get("source_name"),
        "source_url": item.get("source_url"),
        "external_id": external_id,
        "published_at": item.get("published_at"),
        "excerpt": (item.get("content") or "")[:280],
    }, on_conflict="report_id,source_id,external_id").execute()

    patch: dict = {"published_at": datetime.now(timezone.utc).isoformat()}
    if not already_attached:
        patch["source_count"] = (report.get("source_count") or 1) + 1
    old = _SEVERITY_RANK.get(report.get("severity") or "low", 0)
    new = _SEVERITY_RANK.get(item.get("severity") or "low", 0)
    if new > old:
        patch["severity"] = item["severity"]

    # "מי שהביא את הדיווח הכי מוקדם זכה" — גם ב"מקור" המוצג, לא רק
    # בזמן. ראה docstring: זה בכוונה לא תלוי בסדר הקליטה בפועל.
    #
    # זו UPDATE מותנית נפרדת, לא שדה בתוך patch הרגיל: מקרה אמיתי
    # שדלף — הודעה מוקדמת (צופר) ומאוחרת (אהרון ידיעות) מאותו אירוע
    # מגיעות כמעט יחד, ושני on_message() רצים כ-tasks מקבילים
    # באותו event loop. שניהם קוראים את אותו report ישן (לפני
    # שהעדכון של אף אחד מהם נכתב), שניהם מחשבים patch לפי אותו
    # מצב-בסיס, ומי שכותב ל-DB אחרון מנצח — גם אם זה המקור המאוחר.
    # תנאי ב-Python על עותק מקומי לא מספיק נגד זה; ה-WHERE כאן
    # נבדק ב-Postgres מול המצב האמיתי ברגע הכתיבה, אז רק המקור
    # שבאמת הכי מוקדם יכול לנצח, בלי קשר לסדר העיבוד.
    new_published = _parse_time(item.get("published_at"))
    if new_published:
        # רשת ביטחון: "מוקדם יותר זוכה" רק בפער סביר (עד 24 שעות),
        # לא בלי גבול. דלף בפועל: פרגמנט לא קשור ממש (ראה find_duplicate
        # ו-find_latest_report) עם published_at מ-12 יום קודם דרס את
        # occurred_at ואת פרטי ה"מקור" המוצגים של אירוע אחר לגמרי,
        # וגרם לכרטיס להציג "לפני 12 ימים" על אירוע שקרה היום. גם
        # אחרי שהמיזוג השגוי עצמו תוקן (find_latest_report), זו הגנה
        # נוספת ישירות כאן — "מוקדם" לא אמור אף פעם להיות רחוק כל כך.
        earliest_plausible = (new_published + timedelta(hours=24)).isoformat()
        db().table("reports").update({
            "occurred_at": item.get("published_at"),
            "source_id": item.get("source_id"),
            "source_name": item.get("source_name"),
            "source_url": item.get("source_url"),
            "external_id": external_id or None,
        }).eq("id", report["id"]).or_(
            f"occurred_at.is.null,and(occurred_at.gt.{item.get('published_at')},occurred_at.lt.{earliest_plausible})"
        ).execute()

    # תוכן חדש מתווסף לדיווח הנראה, לא רק ל-report_sources.excerpt —
    # אחרת שלב חדש בסיפור (אזעקה → יירוט → נפילה בשטח פתוח) נבלע
    # ב-source_count בלי שאף אחד רואה אותו בכרטיס עצמו. append_note
    # (פרגמנט קצר, ראה _is_followup_fragment) תמיד נכנס; מיזוג רגיל
    # נכנס רק אם זה לא כמעט אותו ניסוח על אותה עובדה — אחרת שלושה
    # אתרים שמנסחים את אותו משפט שונה היו מציפים את הכרטיס בחזרות.
    # שורה נפרדת (לא " · עדכון:") כדי שזה ייקרא כרצף עדכונים, לא סלט.
    note = (item.get("content") or "").strip()
    base = (report.get("content") or "").strip()
    already_present = bool(note) and note in base
    near_duplicate = not append_note and note and base and _is_near_duplicate_line(note, base)
    if note and not already_present and not near_duplicate:
        source_label = item.get("source_name")
        time_label = _il_time(item.get("published_at"))
        if source_label and time_label:
            prefix = f"עדכון ({source_label}, {time_label}):"
        elif source_label:
            prefix = f"עדכון ({source_label}):"
        else:
            prefix = "עדכון:"
        patch["content"] = f"{base}\n{prefix} {note}" if base else note

    # "התברר שווא"/"חזרה לשגרה"/"לא נמצא ממצא" — האירוע נסגר. נבדק
    # על תוכן המקור החדש תמיד, לא רק בזרימת ה-append_note, כי דיווח
    # סגירה לרוב מגיע כפסקה מלאה שמאוחדת דרך דמיון טקסטואלי רגיל
    # (find_duplicate) ולא כפרגמנט קצר. חזרה לשגרה במקום דיווח תקוע
    # כ"פעיל" לנצח, ויורד מהמפה (index.html מסנן dismissed משם) בלי
    # להיעלם מרשימת הדיווחים.
    new_text = (item.get("content") or "").strip() or (item.get("title") or "").strip()
    if new_text and _is_resolved(new_text):
        patch["status"] = "dismissed"
        # אירוע שנסגר כבר לא "קריטי" באתר — גם אם תוך כדי היו רגעים
        # קריטיים (יירוט, אזעקות). נכתב אחרי בדיקת ההסלמה למעלה כדי
        # לגבור עליה תמיד, לא רק כשהמקור החדש עצמו בחומרה נמוכה.
        # תקציר AI מתווסף רק ברגע הזה — כשהאירוע נסגר — כי זה הרגע
        # שיש סיפור שלם לסכם. לסכם אחרי כל עדכון היה יוצר תקצירים
        # חלקיים שמוחלפים כל דקה. שתי שורות ריקות מפרידות מהרצף
        # הכרונולוגי, כדי שיהיה ברור שזו תמצית, לא עוד "עדכון".
        #
        # "סיכום AI:" not in base — לא מסכמים פעמיים. דלף בפועל
        # (בשילוב עם באג ה-find_latest_report שתוקן למעלה): שני
        # פרגמנטי סגירה לא קשורים ("האירוע הסתיים" ממלכיה, "סיום
        # אירוע" מאריאל) נדבקו לאותו דיווח, וכל אחד ייצר סיכום AI
        # נפרד — שני בלוקי "סיכום AI:" מלאים באותו כרטיס.
        full_story = patch.get("content", base)
        if "סיכום AI:" in base:
            summary = None
        else:
            try:
                import relevance
                summary = relevance.summarize_event(full_story)
            except Exception as exc:
                summary = None
                log.debug("תקציר AI לאירוע נכשל: %s", exc)
        if summary:
            patch["content"] = f"{full_story}\n\n\nסיכום AI: {summary}"
        patch["severity"] = "low"

    # מדיה מצטברת: כל מקור שמצרף תמונה/סרטון נכנס לגלריה של האירוע,
    # לא מחליף את מה שכבר יש. עד 8 פריטים — מספיק לאירוע עם הרבה
    # מקורות בלי שהעמוד יתנפח.
    new_media = (item.get("raw") or {}).get("media") or []
    if new_media:
        existing_media = list((report.get("raw") or {}).get("media") or [])
        existing_urls = {m.get("url") for m in existing_media}
        for m in new_media:
            if m.get("url") and m["url"] not in existing_urls:
                existing_media.append(m)
                existing_urls.add(m["url"])
        patch["raw"] = {**(report.get("raw") or {}), "media": existing_media[:8]}

    db().table("reports").update(patch).eq("id", report["id"]).execute()


def attach_media_only(report_id: str, media: list[dict]) -> None:
    """מצרף תמונה/סרטון לדיווח קיים בלי טקסט מלווה משלו.

    דלף בפועל: צופר שולח את התרעת צבע אדום, ואז — בהודעה נפרדת,
    כמעט תמיד בלי כיתוב או עם כיתוב קצר מדי — את התמונה/הצילום של
    האירוע. _build_item ב-telegram_source.py זורק הודעה כזו (אין
    בה מספיק טקסט כדי להיחשב תוכן), אז המדיה שלה אבדה לגמרי גם
    כשהיא שייכת בבירור לאירוע שרק נפתח מאותו ערוץ. משתמשים באותו
    היגיון "דיווח אחרון מאותו ערוץ" כמו find_latest_report, לא רק
    מוסיפים לגלריה בלי לבדוק — אין כאן שום דבר שקושר את ההודעה
    לאירוע חוץ מהערוץ שממנו היא הגיעה.
    """
    if not media:
        return
    result = db().table("reports").select("raw").eq("id", report_id).limit(1).execute()
    rows = result.data or []
    if not rows:
        return
    existing_media = list((rows[0].get("raw") or {}).get("media") or [])
    existing_urls = {m.get("url") for m in existing_media}
    for m in media:
        if m.get("url") and m["url"] not in existing_urls:
            existing_media.append(m)
            existing_urls.add(m["url"])
    db().table("reports").update(
        {"raw": {**(rows[0].get("raw") or {}), "media": existing_media[:8]}}
    ).eq("id", report_id).execute()


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
    "חזר לשגרה", "חזרה לשגרה", "לחזור לשגרה", "אזעקת שווא", "כוזב", "כוזבת",
    "בוטלה ההתרעה", "ללא ממצא", "ללא ממצאים", "לא נמצא דבר",
    "לא נמצאו ממצאים", "לא נמצא כל ממצא", "לא אותרו ממצאים",
    "אין חשד לפעילות עוינת", "התברר כי לא", "התברר שמדובר בכוזב",
    # פיקוד העורף — סגירה רשמית של חלון ההתרעה. לא רק כשההתרעה
    # התבררה כשווא: גם אחרי אירוע אמיתי לגמרי (יירוט, נפילה) פיקוד
    # העורף מודיע "האירוע הסתיים" ברגע שהסכנה חלפה — זה מה שאומר
    # שהאירוע כבר לא חי, לא שהוא לא היה אמיתי.
    "האירוע הסתיים", "סיום אירוע",
]


def _is_followup_fragment(title: str, content: str) -> bool:
    """True אם זו רק הודעת סגירה/עדכון קצרה, לא תוכן עצמאי.

    מקרה אמיתי שדלף: הודעת "סיום אירוע" של צופר הגיעה בבאקפיל
    מאוחר בהרבה מהאירוע המקורי (89dfdb52...) — בלי שום מועמד קרוב
    בזמן ל-find_duplicate, ונוצר כרטיס עצמאי חסר הקשר ("האירוע
    הסתיים" — איזה אירוע?). "האירוע הסתיים" היה במפורש בכותרת
    (קצרה, 58 תווים) אבל הבדיקה בדקה רק את content (88 תווים —
    מעל _FOLLOWUP_MAX_CHARS, ולכן לא נתפס גם אילו המילה הייתה שם).
    בודקים את שניהם בנפרד, לא משורשרים — כך שכותרת קצרה עם הסימן
    נתפסת גם כשה-content המלא ארוך מדי.
    """
    for text in (title, content):
        text = (text or "").strip()
        if text and len(text) <= _FOLLOWUP_MAX_CHARS and \
           any(marker in text for marker in _FOLLOWUP_MARKERS + _RESOLVED_MARKERS):
            return True
    return False


def _is_resolved(content: str) -> bool:
    text = (content or "").strip()
    return any(marker in text for marker in _RESOLVED_MARKERS)


STALE_RESOLVE_HOURS = 3


def auto_resolve_stale_reports() -> int:
    """אירועים בלי אף עדכון 3+ שעות — סגירה אוטומטית ל'חזרה לשגרה'.

    לא כל אירוע מסתיים בהודעת סגירה מפורשת ("האירוע הסתיים" וכו',
    ראה _is_resolved) — לפעמים המקורות פשוט מפסיקים לדווח. דיווח
    שאף אחד לא מוסיף לו כלום שעות ארוכות ככל הנראה כבר לא רלוונטי,
    ואין סיבה שהוא ימשיך להופיע כ"פעיל"/חמור על המפה ולהפחיד
    משתמשים על משהו שכבר לא קורה. published_at (לא occurred_at)
    הוא הבדיקה — הוא מה שמתעדכן בכל אישוש (ראה attach_source),
    כלומר "שעה מהעדכון האחרון", לא "שעה מתחילת האירוע".
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALE_RESOLVE_HOURS)).isoformat()
    try:
        result = (
            db().table("reports")
            .update({"status": "dismissed", "severity": "low"})
            .lt("published_at", cutoff)
            .neq("status", "dismissed")
            .execute()
        )
        return len(result.data or [])
    except Exception as exc:
        log.warning("סגירה אוטומטית של אירועים ישנים נכשלה: %s", exc)
        return 0


def find_latest_report(window_minutes: int = 15, source_id: str | None = None,
                        reference_time: datetime | None = None) -> dict | None:
    """הדיווח האחרון שהערוץ הזה עצמו דיווח עליו — יעד לצירוף פרגמנט קצר (ראה למעלה).

    לא מבוסס דמיון טקסטואלי בכוונה. חלון קצר (15 דקות, לא 45) —
    אותו ערוץ שמפרסם "ללא נפגעים" גם בבוקר וגם בצהריים מדווח על שני
    אירועים שונים, לא מאשש פעמיים את אותו אחד. זיהוי אמיתי של "האם
    זה אותו אירוע" דורש הבנת הקשר (מיקום, זמן, תוכן) שהתאמת מחרוזת
    לא נותנת — זו בדיוק המגבלה של גישה היוריסטית טהורה.

    דלף בפועל: הפונקציה החזירה בעבר "הדיווח האחרון שנכתב בכלל",
    בלי סינון לפי ערוץ — ופרגמנט הסגירה של צופר ("האירוע הסתיים
    במלכיה") נדבק לדיווח לגמרי לא קשור על מעצר בג'נין, רק כי הוא
    נוצר לאחרונה יותר. אותו דבר קרה לסגירת אירוע באריאל מערוץ אחר.
    עכשיו מחפשים לפי report_sources — האם *הערוץ הזה עצמו* דיווח
    על משהו בחלון הזמן — ולא מנחשים "הכי אחרון" כשאין source_id
    (לא אמור לקרות בפועל, טלגרם תמיד נותן source_id).

    reference_time: "עכשיו" כברירת מחדל, אבל למדיה עצמאית (ראה
    telegram_source._try_attach_standalone_media) מעבירים את זמן
    ההודעה עצמה — לא את זמן העיבוד שלה. הודעת תמונה שפוספסה ורק
    נסרקה שעות מאוחר יותר לא אמורה להידבק לאירוע *עכשווי* לגמרי לא
    קשור רק כי חלון "15 הדקות האחרונות" נמדד מרגע העיבוד המאוחר.
    """
    now = reference_time or datetime.now(timezone.utc)
    since = (now - timedelta(minutes=window_minutes)).isoformat()
    if source_id:
        recent = (
            db().table("report_sources").select("report_id")
            .eq("source_id", source_id).gte("published_at", since)
            .order("published_at", desc=True).limit(1).execute()
        )
        rows = recent.data or []
        if not rows:
            return None
        result = (
            db().table("reports").select("id,title,content,source_count,severity,raw")
            .eq("id", rows[0]["report_id"]).limit(1).execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    result = (
        db().table("reports").select("id,title,content,source_count,severity,raw")
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
        # occurred_at נשאר קפוא לתמיד על ההודעה הראשונה — מתי האירוע
        # *קרה* בפועל. published_at, לעומתו, ממשיך לזוז קדימה בכל
        # אישוש נוסף (attach_source) כדי לשמור על החלון של 24 שעות
        # ב-get_reports_for_user ועל דירוג הטריות בפיד. בלי ההפרדה
        # הזו, כרטיס על אזעקה שהתחילה 11:33 והסתיימה 11:44 היה מציג
        # למשתמש "11:44" כאילו זה מתי שזה קרה.
        "occurred_at": item.get("published_at"),
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
        # נרשם *לפני* שיודעים אם זה ייכנס כדיווח עצמאי או יתמזג —
        # כדי שגם דיווח שאדמין ימחק אחר כך לא ייקלט שוב בסריקה הבאה.
        mark_seen(item["source_id"], str(item["external_id"]))

    if _is_followup_fragment(item.get("title"), item.get("content")):
        latest = find_latest_report(
            source_id=item.get("source_id"),
            reference_time=_parse_time(item.get("published_at")),
        )
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
        item.get("published_at"),
    )
    if existing:
        attach_source(existing, item)
        return "deduped"

    return "inserted" if insert_report(item) else "skipped"
