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
        "מוגש מטעם", "שיתוף פעולה מסחרי", "במימון", "ממומן",
        "המלצת המערכת", "כדאי לכם", "הדיל היומי", "בלעדי לגולשים",
    ],
    "מזג אוויר": [
        "מזג האוויר", "תחזית", "גשם", "שלג", "שרב", "התחממות",
        "הטמפרטורות", "מעונן", "בהיר", "סופה", "גל חום", "משקעים",
        "התקררות", "רוחות", "ערפל", "חמסין", "מפולת שלגים",
    ],
    "תאונות דרכים": [
        "תאונת דרכים", "תאונה קטלנית", "נהרג בתאונה", "נפגע בתאונה",
        "התנגשות בין רכבים", "רכב התהפך", "הולך רגל נפגע",
        "תאונת שרשרת", "פגע וברח", "אופנוען נפגע", "נדרס למוות",
        "תאונת עבודה", "נפל מגובה", "טבע בים", "טביעה בבריכה",
        "קורקינט", "רוכב קורקינט", "אופניים חשמליים", "רוכב אופניים",
        "קטנוע", "נפצע קל בתאונה", "פונה לבית החולים לאחר שנפגע",
    ],
    "ספורט": [
        "מכבי תל אביב", "הפועל", "ליגת העל", "הפרמייר ליג", "שער בדקה",
        "הנבחרת", "אליפות", "משחק הגמר", "היורוליג", "כדורגל",
        "כדורסל", "אולימפיאדה", "מונדיאל", "בית'ר", "מכבי חיפה",
        "אצטדיון", "המאמן", "השחקן", "עונת המשחקים", "פלייאוף",
        "הליגה הלאומית", "גביע המדינה", "צהוב", "כרטיס אדום",
        "הרכב פותח", "טורניר", "מדליה", "אלוף", "יורו 20", "ניצחון בליגה",
    ],
    "בידור ולייפסטייל": [
        "האח הגדול", "הישרדות", "כוכב נולד", "רייטינג", "זוכה התוכנית",
        "הורוסקופ", "מזל בתולה", "מזל טלה", "מזל שור", "מזל תאומים",
        "מזל סרטן", "מזל אריה", "מזל מאזניים", "מזל עקרב", "מזל קשת",
        "מזל גדי", "מזל דלי", "מזל דגים", "אסטרולוג", "קלפי טארוט",
        "מתכון", "דיאטה", "טיפים ל", "אופנה", "איפור", "עיצוב הבית",
        "סלבריטי", "הזמר", "הזמרת", "השחקנית", "ריאליטי", "רומן חדש",
        "חתונה של", "התארסו", "נפרדו", "בהריון", "קליפ חדש",
        "אלבום חדש", "פודקאסט", "נטפליקס", "סדרה חדשה", "ביקורת סרט",
        "פרשת השבוע", "בדיחה", "ויראלי", "טיקטוק", "אינסטגרם של",
    ],
    "כלכלה שוטפת": [
        "מדד המחירים", "שער הדולר", "הבורסה ננעלה", "מניית",
        "דוחות כספיים", "הנפקה", "ריבית בנק ישראל", "אינפלציה",
        "מחירי הדירות", "משכנתא", "קרן פנסיה", "השקעות", "ביטקוין",
        "קריפטו", "הייטק גייס", "אקזיט", "רבעון", "תשואה",
    ],
    "פשיעה אזרחית": [
        "רצח", "נרצח", "נרצחה", "חשד לרצח", "מקרה רצח", "גופה",
        "חיסול פלילי", "רקע פלילי", "סכסוך עסקים", "ארגון פשיעה",
        "עבריין", "עבריינים", "שוד", "שודד", "פריצה לדירה",
        "סחיטה באיומים", "הצתת רכב", "אלימות במשפחה", "תקיפה מינית",
        "הטרדה מינית", "גניבת רכב", "הונאה", "מרמה", "סמים",
        "מעצר חשוד ברצח", "כתב אישום",
        # דקירה ואלימות ללא הקשר ביטחוני — סכסוך, שכונה, משפחה.
        # עם הקשר ביטחוני הן עוברות; ראה _SECURITY_CONTEXT.
        "דקירה", "נדקר", "נדקרה", "דקר", "אירוע דקירה", "קטטה",
        "תגרה", "אלימות", "תקיפה חמורה", "איומים",
        # עבירות מין
        "עבירות מין", "עבירת מין", "מעשה מגונה", "אונס", "אנס",
        "פדופיל", "ניצול מיני", "פגיעה מינית", "הטרדות",
    ],
    "בריאות וצרכנות": [
        "מחקר חדש מגלה", "תסמינים", "חיסון שפעת", "קופת חולים",
        "המלצות תזונה", "כושר", "אימון", "שינה טובה", "ויטמין",
        "מוצר חדש", "השוואת מחירים", "ריקול", "חופשה מושלמת",
        "יעד תיירותי", "טיסות זולות",
    ],
}

# חוקים נוספים שנטענים מטבלת filter_rules ב-DB, כדי שתוכל
# לחסום נושא חדש בלי לחכות לעדכון קוד.
_DB_RULES: dict[str, list[str]] = {}


# שלושת סוגי החוקים שהטבלה מתירה. הסכמה תוכננה נכון מלכתחילה:
# לא רק חסימה, אלא גם קידום ודגל לבדיקה.
_PROMOTE: list[str] = []   # התאמה → נשמר ומקודם לראש הפיד
_REVIEW: list[str] = []    # התאמה → נשמר אך מסומן כדורש בדיקה


def load_db_rules() -> None:
    """טוען את שלושת סוגי החוקים מטבלת filter_rules.

      suppress — חוסם. מצטרף לרשימות שבקוד.
      promote  — מבטיח שהדיווח נשמר, ומסמן אותו לקידום בדירוג.
      review   — נשמר, אך מקבל status='reviewing' לבדיקה ידנית.

    נכשל בשקט: אם הטבלה חסרה, חוקי הקוד ממשיכים לעבוד.
    """
    global _DB_RULES, _COMPILED_BLOCKS, _PROMOTE, _REVIEW
    try:
        from store import db
        rows = (db().table("filter_rules").select("*")
                .eq("is_active", True).execute().data or [])
    except Exception as exc:
        log.debug("filter_rules לא נטענו: %s", exc)
        return

    def words_of(row: dict) -> list[str]:
        raw = row.get("keywords")
        if isinstance(raw, list):
            return [str(w).strip() for w in raw if str(w).strip()]
        if isinstance(raw, str) and raw.strip():
            return [w.strip() for w in raw.split(",") if w.strip()]
        return []

    suppress: dict[str, list[str]] = {}
    promote, review = [], []
    for row in rows:
        kind = str(row.get("rule_type") or "suppress").strip().lower()
        words = words_of(row)
        if not words:
            continue
        if kind == "promote":
            promote += words
        elif kind == "review":
            review += words
        else:
            label = str(row.get("name") or "מותאם אישית").strip()
            suppress.setdefault(label, []).extend(words)

    _PROMOTE = [normalize_for_match(w) for w in promote]
    _REVIEW = [normalize_for_match(w) for w in review]
    if suppress:
        _DB_RULES = suppress
        _COMPILED_BLOCKS = _compile_blocks()
    log.info("חוקי סינון: %d חסימה · %d קידום · %d בדיקה",
             sum(len(v) for v in suppress.values()), len(_PROMOTE), len(_REVIEW))


def matches_promote(text: str) -> bool:
    haystack = normalize_for_match(text)
    return any(w and w in haystack for w in _PROMOTE)


def matches_review(text: str) -> bool:
    haystack = normalize_for_match(text)
    return any(w and w in haystack for w in _REVIEW)


def _compile_blocks() -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {k: list(v) for k, v in BLOCK_RULES.items()}
    for cat, kws in _DB_RULES.items():
        merged.setdefault(cat, []).extend(kws)
    return {c: [normalize_for_match(k) for k in kws] for c, kws in merged.items()}


_COMPILED_BLOCKS = _compile_blocks()


# קטגוריות שבהן מילת החסימה עמומה: "ירי" ו"רצח" מופיעים גם
# בפיגוע וגם בסכסוך פלילי. בהן נדרש לוודא שאין הקשר ביטחוני
# לפני שזורקים — אחרת פיגוע ירי ייחסם כאירוע פלילי.
_AMBIGUOUS = {"פשיעה אזרחית", "תאונות ואירועים אזרחיים", "תאונות אזרחיות"}

_SECURITY_CONTEXT = [
    "מחבל", "מחבלים", "פיגוע", "טרור", "חוליית טרור", "שבכ", "שב\"כ",
    "כוחות הביטחון", "צהל", "צה\"ל", "מגב", "מג\"ב", "לוחמים",
    "כתת כוננות", "התארגנות עוינת", "לאומני", "רקע לאומני",
    "יידוי אבנים", "בקבוק תבערה", "מטען", "חדירה", "התנחלות",
    "יהודה ושומרון", "בשומרון", "בבנימין", "מאחז", "צומת",
]


def hard_block(text: str) -> str | None:
    """מחזיר את שם הקטגוריה החסומה, או None אם עבר."""
    haystack = normalize_for_match(text)
    security = any(normalize_for_match(k) in haystack for k in _SECURITY_CONTEXT)
    for category, keywords in _COMPILED_BLOCKS.items():
        for keyword in keywords:
            if keyword and keyword in haystack:
                if category in _AMBIGUOUS and security:
                    # "ירי ברכב, החשוד נעצר" נחסם.
                    # "פיגוע ירי, המחבל נוטרל" עובר.
                    continue
                return category
    return None


# ─────────────────────────────────────────────────────────────
# מסנן 2 · שיקול דעת עריכתי
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """אתה עורך חדשות בטחוני במערכת OSINT ישראלית.
תפקידך להחליט אילו ידיעות מגיעות ללוח המחוונים, ולסכם אותן.

הקו העריכתי: הלוח מציג אירועים שמשפיעים ישירות על ישראלים.

לכלול (relevant=true), לפי סדר חשיבות יורד:
1. שיגורים לעבר ישראל: טילים בליסטיים, רקטות, כטב"מים ורחפנים
   מאיראן, תימן, לבנון, עזה או סוריה. זו העדיפות הראשונה.
2. פיגועים, ובמיוחד ביהודה ושומרון: ירי, דקירה, דריסה, מטענים,
   יידוי אבנים ובקבוקי תבערה לעבר ישראלים
3. אירועי ירי מתפרצים ברקע ביטחוני או לאומני
4. אזעקות, צבע אדום והנחיות פיקוד העורף
5. אירועי גבול הנוגעים לישראל: חדירות, ניסיונות חטיפה, כוננות
6. מעצרי מחבלים, סיכול פיגועים, התארגנויות עוינות
7. החלטות מדיניות־ביטחוניות עם השפעה ישירה על ביטחון ישראלים

לא לכלול (relevant=false):
- פעילות צה"ל בחו"ל כשלעצמה: תקיפות בעזה, לבנון או סוריה שאין בהן
  פגיעה בישראלים או איום עליהם. זה עיקר ההבדל — "צה״ל תקף בעזה"
  נזרק, אבל "ירי רקטות מעזה לעבר שדרות" נשמר
- תאונות דרכים, מזג אוויר, תוכן ממומן או שיווקי
- ספורט, בידור, לייפסטייל, בריאות, מתכונים
- כלכלה ושוק ההון שלא קשורים לביטחון
- חדשות חוץ בלי זיקה לישראל
- פוליטיקה פנימית, בחירות, קואליציה — אלא אם יש אירוע ביטחוני ממשי
- פשיעה אזרחית: רצח על רקע פלילי, סכסוכי עבריינים, שוד, אלימות
  במשפחה, עבירות מין, סמים. שים לב להבחנה — "ירי ברכב על רקע
  פלילי" נזרק, אבל "פיגוע ירי" או "אירוע ירי על רקע לאומני" נשמר.
  הרקע הוא שקובע, לא סוג האירוע

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
    # שיגורים לעבר ישראל — העדיפות הראשונה
    "שיגור", "שוגר", "שוגרו", "טיל בליסטי", "טילים ששוגרו", "מטח טילים",
    "שיגור לעבר", "כטבם", "כטב\"מ", "רחפן עוין", "כלי טיס עוין",
    "נפילה", "נפילת רקטה", "פגיעה ישירה", "רסיסים",
    # פיגועים
    "פיגוע", "פיגוע ירי", "פיגוע דקירה", "פיגוע דריסה", "מחבל",
    "מחבלים", "נוטרל", "חוליית טרור", "התארגנות עוינת", "רקע לאומני",
    "אירוע ירי", "ירי לעבר כלי רכב", "יידוי אבנים", "בקבוק תבערה",
    # מקור מתמשך
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

    promoted = matches_promote(text)

    # promote *אינו* עוקף מוחלט. הגרסה הקודמת נתנה לו לגבור על כל
    # חסימה, ועם 79 מילות קידום בטבלה כמעט הכל קודם — דקירה פלילית
    # בבית שמש עברה כי "דקירה" הופיעה באחד מחוקי הקידום.
    #
    # עכשיו: חסימה מוחלטת (ספורט, מזג אוויר, ממומן) גוברת תמיד.
    # בקטגוריות העמומות — פשיעה ותאונות — מה שמכריע הוא הקשר
    # ביטחוני בטקסט עצמו, לא מילה ברשימת קידום. "דקירה" לבדה היא
    # אירוע פלילי; "פיגוע דקירה" או דקירה עם מחבל היא לא.
    blocked = hard_block(text)
    if blocked:
        return False, f"חסום · {blocked}", item

    if matches_review(text):
        item["status"] = "reviewing"

    if promoted:
        item["raw"] = (item.get("raw") or {}) | {"promoted": True}

    if not ANTHROPIC_API_KEY:
        keep, reason = heuristic_relevance(text)
        if promoted:
            keep, reason = True, "קודם · חוק promote"
        item["content"] = trim_summary(item.get("content", ""))
        item["raw"] = (item.get("raw") or {}) | {"filter": "heuristic"}
        return keep, reason, item

    verdict = _call_api(text)
    if verdict is None and promoted:
        item["content"] = trim_summary(item.get("content", ""))
        return True, "קודם · חוק promote", item
    if verdict is None:
        # ה-API נפל — לא זורקים דיווחים בגלל תקלה זמנית
        keep, reason = heuristic_relevance(text)
        item["content"] = trim_summary(item.get("content", ""))
        item["raw"] = (item.get("raw") or {}) | {"filter": "heuristic-fallback"}
        return keep, f"{reason} (AI לא זמין)", item

    if not verdict.get("relevant") and not promoted:
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
        "promoted": promoted or None,
    }
    label = verdict.get("category", "רלוונטי")
    return True, f"AI · {label}" + (" · קודם" if promoted else ""), item
