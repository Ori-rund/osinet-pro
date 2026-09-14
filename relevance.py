"""
שכבת הסינון — מה נכנס לאתר ומה נזרק.

המבנה הוא שני מסננים בשרשרת, וההפרדה ביניהם מכוונת:

  1. מסנן זול (חינם, מיידי) — שפה וחסימות ברורות.
     תוכן ממומן, מזג אוויר, ספורט, הורוסקופ. אין כאן שיקול דעת,
     רק התאמת דפוסים, ולכן הוא רץ ראשון וחוסך קריאות API.

  2. מסנן AI (עולה אגורות) — שיקול דעת עריכתי + סיכום.
     רק על מה ששרד את השלב הראשון.

למה צריך את השני: הקו העריכתי כאן לא ניתן לביטוי במילות מפתח.
"תקיפת צה״ל בעזה" נזרק, אבל "ירי רקטות מעזה לעבר שדרות" נשמר —
שתיהן מכילות "עזה". ההבדל הוא מי מותקף, וזו הבנה ולא התאמת מחרוזת.
בלי מפתח API המערכת נופלת להיוריסטיקה שמנסה לקרב את זה; היא
סבירה, אבל תפספס בדיוק במקרים הגבוליים.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

from enrich import normalize_for_match

log = logging.getLogger("filter")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
MODEL = os.getenv("FILTER_MODEL", "claude-haiku-4-5-20251001")
API_URL = "https://api.anthropic.com/v1/messages"

MIN_HEBREW_RATIO = 0.25   # מתחת לזה — לא באמת טקסט עברי
SUMMARY_MAX_CHARS = 320   # שלוש שורות בערך


# ─────────────────────────────────────────────────────────────
# מסנן 1 · שפה
# ─────────────────────────────────────────────────────────────
_HEBREW = re.compile(r"[֐-׿]")
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


def is_hebrew(text: str) -> bool:
    """יחס אותיות עבריות מכלל האותיות.

    בדיקה של 'יש אות עברית אחת' לא מספיקה — כתבה באנגלית עם שם
    ישראלי אחד תעבור. יחס מינימלי הוא מה שבאמת מפריד.
    """
    letters = _LETTERS.findall(text or "")
    if len(letters) < 10:
        return False
    hebrew = sum(1 for ch in letters if _HEBREW.match(ch))
    return (hebrew / len(letters)) >= MIN_HEBREW_RATIO


# ─────────────────────────────────────────────────────────────
# מסנן 1 · חסימות ברורות
# ─────────────────────────────────────────────────────────────
BLOCK_RULES: dict[str, list[str]] = {
    "ממומן": [
        "בשיתוף", "תוכן שיווקי", "תוכן מקודם", "בחסות", "פרסומת",
        "מקודם", "כתבה ממומנת", "בשיתוף פעולה עם", "sponsored",
        "מבצע בלעדי", "קוד קופון", "הנחה בלעדית", "לרכישה באתר",
        "מוגש מטעם", "פרסום", "שיתוף פעולה מסחרי",
    ],
    "מזג אוויר": [
        "מזג האוויר", "תחזית", "גשם", "שלג", "שרב", "התחממות",
        "הטמפרטורות", "מעונן", "בהיר", "סופה", "גל חום", "משקעים",
    ],
    "תאונות דרכים": [
        "תאונת דרכים", "תאונה קטלנית", "נהרג בתאונה", "נפגע בתאונה",
        "התנגשות בין רכבים", "רכב התהפך", "הולך רגל נפגע",
        "תאונת שרשרת", "פגע וברח", "אופנוען נפגע",
    ],
    "ספורט": [
        "מכבי תל אביב", "הפועל", "ליגת העל", "הפרמייר ליג", "שער בדקה",
        "הנבחרת", "אליפות", "משחק הגמר", "היורוליג", "כדורגל",
        "כדורסל", "אולימפיאדה", "מונדיאל",
    ],
    "בידור ולייפסטייל": [
        "האח הגדול", "הישרדות", "כוכב נולד", "רייטינג", "זוכה התוכנית",
        "הורוסקופ", "מזל", "מתכון", "דיאטה", "טיפים ל", "אופנה",
        "סלבריטי", "ראיון בלעדי עם הזמר", "רומן חדש", "חתונה של",
    ],
    "כלכלה שוטפת": [
        "מדד המחירים", "שער הדולר", "הבורסה ננעלה", "מניית",
        "דוחות כספיים", "הנפקה", "ריבית בנק ישראל", "אינפלציה",
    ],
}

_COMPILED_BLOCKS = {
    category: [normalize_for_match(k) for k in keywords]
    for category, keywords in BLOCK_RULES.items()
}


def hard_block(text: str) -> str | None:
    """מחזיר את שם הקטגוריה החסומה, או None אם עבר."""
    haystack = normalize_for_match(text)
    for category, keywords in _COMPILED_BLOCKS.items():
        for keyword in keywords:
            if keyword and keyword in haystack:
                return category
    return None


# ─────────────────────────────────────────────────────────────
# מסנן 2 · שיקול דעת עריכתי
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """אתה עורך חדשות בטחוני במערכת OSINT ישראלית.
תפקידך להחליט אילו ידיעות מגיעות ללוח המחוונים, ולסכם אותן.

הקו העריכתי: הלוח מציג אירועים שמשפיעים ישירות על ישראלים.

לכלול (relevant=true):
- תקיפות על ישראל: רקטות, כטב"מים, טילים מאיראן, תימן, לבנון, עזה, סוריה
- פיגועים בישראל וביהודה ושומרון: ירי, דקירה, דריסה, מטענים
- אזעקות, הנחיות פיקוד העורף, צבע אדום
- אירועי ביטחון בגבולות שנוגעים לישראל: חדירות, ניסיונות חטיפה
- אירועי ביטחון פנים: מהומות, עימותים אלימים, מעצרי מחבלים
- החלטות מדיניות־ביטחוניות עם השפעה ישירה על ביטחון ישראלים

לא לכלול (relevant=false):
- פעילות צה"ל בחו"ל כשלעצמה: תקיפות בעזה, לבנון או סוריה שאין בהן
  פגיעה בישראלים או איום עליהם. זה עיקר ההבדל — "צה״ל תקף בעזה"
  נזרק, אבל "ירי רקטות מעזה לעבר שדרות" נשמר
- תאונות דרכים, מזג אוויר, תוכן ממומן או שיווקי
- ספורט, בידור, לייפסטייל, בריאות, מתכונים
- כלכלה ושוק ההון שלא קשורים לביטחון
- חדשות חוץ בלי זיקה לישראל
- פוליטיקה פנימית, בחירות, קואליציה — אלא אם יש אירוע ביטחוני ממשי

severity:
  critical — הרוגים, פיגוע מתמשך, מטח כבד, אירוע רב־נפגעים
  high     — פצועים, אזעקות, יירוט, תקיפה פעילה
  medium   — חשד, מעצר, עימותים, הפרעה משמעותית
  low      — סיכום, עדכון, חזרה לשגרה

summary: סיכום בעברית, שתיים עד שלוש שורות, עד 300 תווים.
עובדות בלבד: מה קרה, איפה, מה ההיקף. בלי מליצות ובלי ספקולציה.
בלי לפתוח ב"הידיעה מדווחת" — ישר לעניין.

category: אחת מ: תקיפה, פיגוע, אזעקות, גבול, ביטחון פנים, מדיני, אחר

החזר JSON בלבד, בלי טקסט נוסף:
{"relevant": bool, "reason": "נימוק קצר", "category": "...", "severity": "...", "summary": "..."}"""


def _call_api(text: str, timeout: float = 20.0) -> dict | None:
    """קריאה אחת ל-API. מחזיר None בכל כשל — הקורא נופל להיוריסטיקה."""
    try:
        response = httpx.post(
            API_URL,
            timeout=timeout,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 400,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text[:2000]}],
            },
        )
        response.raise_for_status()
        body = response.json()["content"][0]["text"].strip()
    except Exception as exc:
        log.warning("קריאת סיווג נכשלה · %s", exc)
        return None

    # המודל עלול לעטוף ב-```json למרות ההנחיה
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body).strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", body, re.DOTALL)
        if not match:
            log.warning("תשובת סיווג לא תקינה · %s", body[:120])
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    return parsed if isinstance(parsed, dict) else None


# ─────────────────────────────────────────────────────────────
# נפילה ללא AI
# ─────────────────────────────────────────────────────────────
# סימנים שהאירוע נוגע לישראל ישירות
_ISRAEL_IMPACT = [
    "אזעקה", "אזעקות", "צבע אדום", "פיקוד העורף", "יירוט", "יורט",
    "נפילת רקטה", "נפלה רקטה", "מטח", "כטבם", "כטב\"מ", "רחפן",
    "פיגוע", "דקירה", "דריסה", "ירי לעבר", "חדירת מחבלים",
    "מטען חבלה", "חטיפה", "מרחב מוגן", "ממד", "חדירה לשטח ישראל",
    "לעבר ישראל", "לעבר יישובי", "בשטח ישראל", "אירוע ביטחוני",
    "כוננות", "התרעה", "נפגעים", "כתת כוננות",
]

# תקיפות שלנו בחוץ — נזרקות אלא אם יש סימן פגיעה בישראל
_OUTBOUND_STRIKE = [
    "צהל תקף", "צהל פעל", "חיל האוויר תקף", "תקיפה בעזה",
    "תקיפות בעזה", "תקיפה בלבנון", "תקיפות בלבנון", "תקיפה בסוריה",
    "תקיפה בתימן", "חיסל את", "חוסל בתקיפה", "פעילות בצפון הרצועה",
    "כוחות פועלים ב", "תמרון קרקעי",
]


def heuristic_relevance(text: str) -> tuple[bool, str]:
    """החלטת רלוונטיות בלי AI. גסה, אבל שומרת על הקו העריכתי."""
    haystack = normalize_for_match(text)
    impact = any(normalize_for_match(k) in haystack for k in _ISRAEL_IMPACT)
    outbound = any(normalize_for_match(k) in haystack for k in _OUTBOUND_STRIKE)

    if impact:
        # פגיעה בישראל גוברת תמיד — גם אם מוזכרת גם תקיפה שלנו
        return True, "סימן פגיעה בישראל"
    if outbound:
        return False, "פעילות בחו״ל בלי פגיעה בישראל"
    return False, "אין סימן לאירוע ביטחוני נוגע לישראל"


def trim_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """קיצור לשלוש שורות בערך, על גבול משפט."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for sentence in sentences:
        if len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    if out:
        return out
    return text[:limit].rsplit(" ", 1)[0] + "…"


# ─────────────────────────────────────────────────────────────
# נקודת הכניסה
# ─────────────────────────────────────────────────────────────
def screen(item: dict) -> tuple[bool, str, dict]:
    """מחליט אם הדיווח נכנס לאתר, ומעשיר אותו אם כן.

    מחזיר (לשמור, סיבה, פריט מעודכן).
    """
    text = f"{item.get('title', '')}. {item.get('content', '')}".strip()

    if not is_hebrew(text):
        return False, "לא עברית", item

    blocked = hard_block(text)
    if blocked:
        return False, f"חסום · {blocked}", item

    if not ANTHROPIC_API_KEY:
        keep, reason = heuristic_relevance(text)
        item["content"] = trim_summary(item.get("content", ""))
        item["raw"] = (item.get("raw") or {}) | {"filter": "heuristic"}
        return keep, reason, item

    verdict = _call_api(text)
    if verdict is None:
        # ה-API נפל — לא זורקים דיווחים בגלל תקלה זמנית
        keep, reason = heuristic_relevance(text)
        item["content"] = trim_summary(item.get("content", ""))
        item["raw"] = (item.get("raw") or {}) | {"filter": "heuristic-fallback"}
        return keep, f"{reason} (AI לא זמין)", item

    if not verdict.get("relevant"):
        return False, f"AI · {verdict.get('reason', 'לא רלוונטי')}", item

    summary = trim_summary(verdict.get("summary") or item.get("content", ""))
    if summary:
        item["content"] = summary
    if verdict.get("severity") in ("critical", "high", "medium", "low"):
        item["severity"] = verdict["severity"]

    item["raw"] = (item.get("raw") or {}) | {
        "filter": "ai",
        "category": verdict.get("category"),
        "reason": verdict.get("reason"),
    }
    return True, f"AI · {verdict.get('category', 'רלוונטי')}", item
