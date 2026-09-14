"""
בדיקות שכבת הסינון. הרצה: python test_relevance.py

רץ במצב היוריסטיקה (בלי ANTHROPIC_API_KEY) כדי שהבדיקות יהיו
דטרמיניסטיות ולא יעלו כסף. הסימון [AI] מציין מקרים שההיוריסטיקה
מתקשה בהם ושהמפתח נועד לפתור.
"""

import os

os.environ.pop("ANTHROPIC_API_KEY", None)

from relevance import hard_block, heuristic_relevance, is_hebrew, screen, trim_summary

# (טקסט, האם לשמור, הערה)
CASES = [
    # ── חייב להישמר ──
    ("אזעקות הופעלו בשדרות ובעוטף עזה, פיקוד העורף הנחה להיכנס למרחב מוגן", True, ""),
    ("יירוט כטב\"מ ששוגר מתימן לעבר אילת", True, ""),
    ("פיגוע ירי בצומת גוש עציון, שני פצועים פונו לבית החולים", True, ""),
    ("שוגרו טילים מאיראן לעבר ישראל, נשמעו פיצוצים במרכז הארץ", True, ""),
    ("חדירת מחבלים ליישוב בגליל, כתת הכוננות הופעלה", True, ""),
    ("ירי רקטות מעזה לעבר שדרות, אין נפגעים", True, "מזכיר עזה אבל ישראל מותקפת"),

    # ── חייב להיחסם ──
    ("צה\"ל תקף מטרות טרור ברצועת עזה במהלך הלילה", False, "תקיפה שלנו בחוץ"),
    ("חיל האוויר תקף בדרום לבנון, לפי דיווחים ערביים", False, "תקיפה שלנו בחוץ"),
    ("תאונת דרכים קטלנית בכביש 6, הנהג נהרג במקום", False, "תאונת דרכים"),
    ("תחזית מזג האוויר: התחממות קלה והטמפרטורות יעלו", False, "מזג אוויר"),
    ("בשיתוף מאסטרקארד: המבצע שישנה לכם את החופשה", False, "ממומן"),
    ("מכבי תל אביב ניצחה בליגת העל", False, "ספורט"),
    ("הורוסקופ שבועי: מה מחכה למזל בתולה", False, "בידור"),
    ("מדד המחירים לצרכן עלה ב-0.3 אחוזים", False, "כלכלה"),
    ("Rocket sirens sounded in northern Israel this morning", False, "אנגלית"),
]


def main() -> int:
    failures = 0

    print("── סינון מלא " + "─" * 52)
    for text, should_keep, note in CASES:
        keep, reason, _ = screen({"title": text, "content": ""})
        ok = keep == should_keep
        failures += not ok
        mark = "✓" if ok else "✗"
        verdict = "נשמר " if keep else "נחסם "
        suffix = f"  ({note})" if note else ""
        print(f"  {mark} {verdict} {reason[:26]:<26} {text[:40]}{suffix}")

    print("\n── זיהוי שפה " + "─" * 52)
    for text, expected in [
        ("אזעקות הופעלו בשדרות הבוקר", True),
        ("Sirens sounded in Sderot this morning", False),
        ("IDF said the strike in Gaza was precise", False),
        ("דיווח: Ynet מוסר כי האירוע הסתיים", True),
    ]:
        got = is_hebrew(text)
        ok = got == expected
        failures += not ok
        print(f"  {'✓' if ok else '✗'} {'עברית' if got else 'לועזית'}  {text[:48]}")

    print("\n── קיצור לשלוש שורות " + "─" * 44)
    long = ("כוחות הביטחון פועלים באזור בעקבות הדיווח. המשטרה הודיעה כי הכבישים "
            "נחסמו לתנועה. תושבים התבקשו להימנע מהגעה לאזור. דובר צה\"ל מסר כי "
            "הכוחות סורקים את השטח. בהמשך יימסרו פרטים נוספים על האירוע ועל "
            "היקף הנזק שנגרם למבנים באזור.")
    short = trim_summary(long)
    ok = len(short) <= 320 and short.endswith((".", "…"))
    failures += not ok
    print(f"  {'✓' if ok else '✗'} {len(long)} → {len(short)} תווים")
    print(f"     {short}")

    print("\n" + ("✅ הכל עבר" if not failures else f"❌ {failures} כשלים"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
