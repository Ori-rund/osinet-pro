"""
שכבת ההעשרה — מה שהופך הודעת טלגרם גולמית לדיווח מובנה.

שלושה דברים קורים כאן:
  1. ניקוי ונרמול של טקסט עברי
  2. חילוץ מיקום (→ סיכה על המפה) וחומרה (→ צבע הסיכה)
  3. חישוב dedup_key כדי שאותו אירוע מ-8 ערוצים יהיה כרטיס אחד

הסיווגים כאן הם היוריסטיקות מבוססות מילות מפתח. הן מכסות הרבה ועולות
אפס, אבל הן לא מבינות הקשר או שלילה ("אין נפגעים" מול "יש נפגעים").
השדרוג הנכון הוא מעבר סיווג עם מודל — ראה classify_with_llm בתחתית.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone

from gazetteer import PLACES, all_terms

_TERMS = all_terms()

# ניקוד, טעמים וסימני עיצוב נסתרים
_NIQQUD = re.compile(r"[֑-ׇ]")
_INVISIBLE = re.compile(r"[​-‏‪-‮﻿]")
_URL = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⬀-⯿]",
    flags=re.UNICODE,
)
_MULTISPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w֐-׿\s]")

# רעש שחוזר בכל ערוץ טלגרם ומזהם את הכותרת ואת ה-dedup
_BOILERPLATE = [
    r"שלחו לנו ידיעות.*$",
    r"להצטרפות לערוץ.*$",
    r"לחצו כאן להצטרפות.*$",
    r"הצטרפו לערוץ.*$",
    r"שיתוף בוואטסאפ.*$",
    r"@\w+",
    r"#\w+",
    r"\|\s*צילום:.*$",
    r"צילום:\s*\S+\s*$",
]
_BOILERPLATE_RE = [re.compile(p, re.MULTILINE) for p in _BOILERPLATE]


# ─────────────────────────────────────────────────────────────
# נרמול טקסט
# ─────────────────────────────────────────────────────────────
def clean_text(raw: str) -> str:
    """מנקה הודעה גולמית לטקסט קריא, בלי לאבד את המשמעות."""
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw)
    text = _INVISIBLE.sub("", text)
    text = _NIQQUD.sub("", text)
    for pattern in _BOILERPLATE_RE:
        text = pattern.sub("", text)
    text = _URL.sub("", text)
    text = _EMOJI.sub(" ", text)
    text = _MULTISPACE.sub(" ", text)
    return text.strip()


def normalize_for_match(text: str) -> str:
    """נרמול אגרסיבי — רק להשוואה, לא לתצוגה."""
    text = clean_text(text)
    text = _PUNCT.sub(" ", text)
    # גרשיים בראשי תיבות עבריים: ת"א → תא
    text = text.replace('"', "").replace("'", "")
    return _MULTISPACE.sub(" ", text).strip().lower()


# אותיות השימוש: בכל"ם + ו + ש. בעברית הן נדבקות למילה עצמה,
# ולכן "בקריית שמונה" לא מכיל את המחרוזת "קריית שמונה".
# זו הסיבה מספר אחת שחילוץ ישויות בעברית נכשל בשקט.
_PREFIX_LETTERS = "בלמכשווה"


def strip_prefix(token: str) -> str:
    """מסיר אות שימוש מובילה. שמרני — לא נוגע במילים קצרות."""
    if len(token) > 3 and token[0] in "בלמכשו":
        stripped = token[1:]
        if len(stripped) > 2:
            return stripped.removeprefix("ה") if len(stripped) > 3 else stripped
    if len(token) > 3 and token[0] == "ה":
        return token[1:]
    return token


def make_title(text: str, max_len: int = 90) -> str:
    """כותרת מהמשפט הראשון. ערוצי טלגרם לא שולחים כותרות."""
    text = clean_text(text)
    if not text:
        return "דיווח ללא כותרת"
    first = re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0].strip()
    if len(first) > max_len:
        cut = first[:max_len].rsplit(" ", 1)[0]
        return cut + "…"
    return first or text[:max_len]


# ─────────────────────────────────────────────────────────────
# חילוץ מיקום
# ─────────────────────────────────────────────────────────────
def _term_pattern(needle: str) -> re.Pattern:
    """בונה ביטוי שסובל אותיות שימוש בתחילת השם.

    "קריית שמונה" צריך לתפוס גם "בקריית שמונה",
    "העיר העתיקה" גם "בעיר העתיקה" — כאן ה"א הידיעה מתחלפת בב'.
    לכן מפרידים את ה"א מהגרעין ומאפשרים כל צירוף לפניו.
    """
    words = needle.split()
    first = words[0]
    core = first[1:] if first.startswith("ה") and len(first) > 3 else first
    rest = (" " + " ".join(words[1:])) if len(words) > 1 else ""

    if len(core) >= 4 or len(words) > 1:
        head = f"[{_PREFIX_LETTERS}]{{0,2}}ה?{re.escape(core)}"
    else:
        # שמות קצרים ("עזה", "צור") — בלי סבילות לתחיליות.
        # "בעזה" שווה לתפוס, אבל "מעזה" בתוך "מעזהיר" יעלה לנו ביוקר.
        head = re.escape(first)

    return re.compile(r"(?:^|\s)" + head + re.escape(rest) + r"(?=$|\s|,|\.)")


_COMPILED: list[tuple[re.Pattern, str]] = [
    (_term_pattern(normalize_for_match(term)), canonical)
    for term, canonical in _TERMS
    if normalize_for_match(term)
]


def extract_location(text: str) -> tuple[str | None, float | None, float | None]:
    """מחזיר (שם מקום, lat, lng) — ההתאמה הארוכה ביותר שנמצאה.

    הסדר קריטי: _TERMS ממוין מהארוך לקצר, כך ש"דרום לבנון" נבדק
    לפני "לבנון" ו"קריית שמונה" לפני "קריית גת".
    """
    if not text:
        return None, None, None
    haystack = normalize_for_match(text)

    for pattern, canonical in _COMPILED:
        if pattern.search(haystack):
            lat, lng, _precision = PLACES[canonical]
            return canonical, lat, lng

    return None, None, None


# ─────────────────────────────────────────────────────────────
# סיווג חומרה
# ─────────────────────────────────────────────────────────────
# הסדר קובע: הבדיקה עוצרת בהתאמה הראשונה, מהחמור לקל.
_SEVERITY_RULES: list[tuple[str, list[str]]] = [
    ("critical", [
        "פיגוע", "חדירת מחבלים", "אירוע ירי", "נפילה ישירה", "הרוג", "הרוגים",
        "נהרג", "נהרגו", "חטיפה", "נחטף", "פצוע אנוש", "במצב אנוש",
        "מטח רקטות", "ירי רקטות", "צבע אדום", "חדירת כלי טיס עוין",
        "מטען חבלה", "פיצוץ מטען", "דקירה", "פיגוע דריסה",
        "נפילת רקטה", "נפלה רקטה", "נפילת כטבם", "פגיעה ישירה",
    ]),
    ("high", [
        "פצועים", "נפצע", "נפצעו", "פצוע קשה", "אזעקה", "אזעקות",
        "יירוט", "יורט", "כתת כוננות", "התקפה", "תקיפה", "הופעל",
        "שריפה גדולה", "התפוצצות", "ירי לעבר", "חשד לחדירה",
        "פינוי מבנים", "נזק לרכוש", "כלי טיס חשוד", "רחפן חשוד",
    ]),
    ("medium", [
        "חשד", "עיכוב", "חסימת כביש", "הפגנה", "מהומות", "עימותים",
        "התפרעות", "מעצר", "נעצר", "נעצרו", "חקירה", "תאונה",
        "שריפה", "תקלה", "הפסקת חשמל", "פיקוד העורף", "הנחיות",
        "כוחות גדולים", "סריקות",
    ]),
    ("low", [
        "חזרה לשגרה", "הסתיים", "האירוע הסתיים", "שווא", "אזעקת שווא",
        "הוסרו ההגבלות", "אין נפגעים", "עדכון", "הודעה", "דיווח ראשוני",
        "ללא נפגעים", "בוטלה",
    ]),
]

# ביטויים שמורידים חומרה גם כשיש מילה חמורה בהודעה
_DEESCALATORS = [
    "אזעקת שווא", "התברר כשווא", "אין נפגעים", "ללא נפגעים",
    "האירוע הסתיים", "חזרה לשגרה", "הוסרו ההגבלות", "האזעקה בוטלה",
]


def classify_severity(text: str, default: str = "low") -> str:
    """חומרה לפי מילות מפתח, עם בלם לביטויי הרגעה."""
    if not text:
        return default
    haystack = normalize_for_match(text)

    deescalated = any(normalize_for_match(p) in haystack for p in _DEESCALATORS)

    for level, keywords in _SEVERITY_RULES:
        for keyword in keywords:
            if normalize_for_match(keyword) in haystack:
                if deescalated and level in ("critical", "high"):
                    # "נשמעו אזעקות — התברר כשווא" הוא לא אירוע חמור
                    return "medium"
                return level
    return default


# ─────────────────────────────────────────────────────────────
# דה-דופליקציה
# ─────────────────────────────────────────────────────────────
# מילות קישור עבריות — נופלות מה-shingle כי הן בכל הודעה
_STOPWORDS = {
    "של", "את", "עם", "על", "אל", "כי", "גם", "או", "אך", "אם", "זה", "זו",
    "היא", "הוא", "הם", "הן", "אני", "אנחנו", "יש", "אין", "לא", "כן",
    "היה", "היתה", "יהיה", "כל", "רק", "עוד", "כבר", "אחרי", "לפני",
    "בין", "לפי", "מתוך", "אצל", "בתוך", "כמו", "מאוד", "יותר", "פחות",
    "עכשיו", "היום", "אתמול", "מחר", "כעת", "בוקר", "ערב", "לילה",
    "דיווח", "עדכון", "הודעה", "פרטים", "בהמשך", "מיד", "כרגע",
}


def _significant_tokens(text: str) -> list[str]:
    """מילים נושאות משמעות, בלי אותיות שימוש.

    הסרת התחיליות היא מה שגורם ל"בקריית" ו"קריית" להיחשב אותה מילה,
    ובלעדיה מדד הדמיון נופל בכל פעם ששני ערוצים מנסחים אחרת.
    """
    tokens = (strip_prefix(t) for t in normalize_for_match(text).split())
    return [t for t in tokens if len(t) > 2 and t not in _STOPWORDS]


# חלון החסימה בשעות. כל הדיווחים מאותו מקום באותו חלון הופכים
# למועמדים לאיחוד; similarity() הוא זה שמכריע בפועל.
BLOCK_HOURS = 3


def dedup_key(text: str, location: str | None = None,
              when: "datetime | None" = None) -> str:
    """מפתח חסימה — לא טביעת אצבע.

    זו הנקודה שבה הגרסה הראשונה של הקוד הזה נכשלה: מפתח שנגזר מכל
    המילים משתנה מכל הבדל ניסוח, ואז שני דיווחים על אותו אירוע לא
    נפגשים לעולם. אז המפתח כאן גס בכוונה — מקום + חלון זמן — והוא
    נועד רק לצמצם את קבוצת המועמדים. ההכרעה היא ב-similarity.

    בלי מיקום נופלים למילים הבולטות, שזה גרוע יותר אבל עדיף מכלום.
    """
    when = when or datetime.now(timezone.utc)
    bucket = int(when.timestamp() // (BLOCK_HOURS * 3600))

    if location:
        seed = f"loc:{location}|t:{bucket}"
    else:
        tokens = _significant_tokens(text)
        # הארוכות ביותר — קירוב עני ל-IDF, אבל יציב
        salient = sorted(sorted(set(tokens), key=len, reverse=True)[:4])
        seed = f"tok:{'|'.join(salient)}|t:{bucket}"

    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def similarity(a: str, b: str) -> float:
    """Jaccard על מילים משמעותיות. 0.0–1.0.

    זה מה שמכריע אם שני דיווחים הם אותו אירוע. מפתח החסימה רק
    מביא את המועמדים; בלי הבדיקה הזו כל שני אירועים באותו יישוב
    באותה שעה היו מתאחדים בטעות.
    """
    set_a, set_b = set(_significant_tokens(a)), set(_significant_tokens(b))
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


# ─────────────────────────────────────────────────────────────
# העשרה מלאה
# ─────────────────────────────────────────────────────────────
def enrich(raw_text: str, *, default_severity: str = "low",
           when: datetime | None = None) -> dict:
    """מהודעה גולמית לשדות שהטבלה reports מצפה להם.

    `when` חייב להיות זמן הפרסום המקורי, לא זמן הקליטה. פיד RSS
    שמחזיר כתבה מלפני שעתיים ייפול אחרת לחלון חסימה שגוי ולא
    יתאחד עם אותו אירוע שהגיע מטלגרם בזמן אמת.
    """
    content = clean_text(raw_text)
    location, lat, lng = extract_location(raw_text)
    return {
        "title": make_title(content),
        "content": content,
        "severity": classify_severity(raw_text, default=default_severity),
        "location_name": location,
        "latitude": lat,
        "longitude": lng,
        "dedup_key": dedup_key(content, location, when),
        "lang": "he" if re.search(r"[֐-׿]", content) else "other",
    }


def classify_with_llm(text: str) -> dict:
    """נקודת ההרחבה לסיווג מבוסס מודל.

    ההיוריסטיקות למעלה טובות ל-80% מהמקרים ונכשלות בדיוק במקרים
    שחשובים: שלילה, ציטוט של אירוע ישן, סרקזם, ודיווח על אירוע
    בחו"ל שמזכיר שם יישוב ישראלי.

    כשנגיע לזה — קריאה אחת ל-API עם prompt שמחזיר JSON מובנה
    (severity, location, entities, is_breaking), עם ההיוריסטיקה
    כ-fallback אם הקריאה נכשלת. עלות: אגורות ל-1000 הודעות.
    """
    raise NotImplementedError("שלב 3 — ראה README")
