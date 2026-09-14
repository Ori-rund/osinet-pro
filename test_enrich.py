"""בדיקות לשכבת ההעשרה. הרצה: python test_enrich.py"""

from enrich import classify_severity, dedup_key, enrich, extract_location, similarity

GEO_CASES = [
    ("נשמעו אזעקות בקריית שמונה ובסביבתה", "קריית שמונה"),
    ("דיווח על ירי לעבר שדרות, פיקוד העורף בבדיקה", "שדרות"),
    ("פיגוע דקירה בעיר העתיקה בירושלים", "העיר העתיקה"),
    ("תקיפה בדרום לבנון לפי דיווחים ערביים", "דרום לבנון"),
    ("עומסי תנועה בכביש 6 צפונה", "כביש 6"),
    ("מזג האוויר מחר יהיה נעים", None),
    ("התרחשות ברמת הגולן סמוך לגבול", "רמת הגולן"),
    ('אירוע ביטחוני בת"א', "תל אביב"),
]

SEVERITY_CASES = [
    ("דיווח על פיגוע ירי, יש נפגעים במקום", "critical"),
    ("נשמעו אזעקות באזור המרכז, יירוט מוצלח", "high"),
    ("הפגנה גדולה חוסמת את הכביש", "medium"),
    ("האירוע הסתיים, חזרה לשגרה", "low"),
    ("נשמעו אזעקות — התברר כאזעקת שווא, אין נפגעים", "medium"),
]

SIM_THRESHOLD = 0.35

DEDUP_PAIRS = [
    # אותו אירוע, ניסוח שונה → צריך להתאחד
    ("דיווח ראשוני: נשמעו אזעקות בקריית שמונה, פיקוד העורף בודק",
     "פיקוד העורף: אזעקות הופעלו בקריית שמונה, הדיווח בבדיקה", True),
    # אותו מקום וחלון זמן, אבל אירועים שונים → אסור להתאחד
    ("נשמעו אזעקות בקריית שמונה, יירוט מוצלח מעל האזור",
     "תאונת דרכים בכניסה לקריית שמונה, שני פצועים קל", False),
]


def main() -> int:
    failures = 0

    print("── חילוץ מיקום " + "─" * 46)
    for text, expected in GEO_CASES:
        name, lat, lng = extract_location(text)
        ok = name == expected
        failures += not ok
        coords = f"{lat:.3f},{lng:.3f}" if lat else "—"
        print(f"  {'✓' if ok else '✗'} {str(name or '—'):<16} {coords:<16} {text[:42]}")

    print("\n── סיווג חומרה " + "─" * 46)
    for text, expected in SEVERITY_CASES:
        got = classify_severity(text)
        ok = got == expected
        failures += not ok
        print(f"  {'✓' if ok else '✗'} {got:<9} (צפוי {expected:<9}) {text[:44]}")

    print("\n── דה-דופליקציה " + "─" * 44)
    for a, b, should_match in DEDUP_PAIRS:
        # מפתח החסימה זהה — שניהם באותו מקום ובאותו חלון זמן.
        # מה שמכריע הוא הדמיון, וזה בדיוק מה שנבדק כאן.
        same_key = dedup_key(a, "קריית שמונה") == dedup_key(b, "קריית שמונה")
        sim = similarity(a, b)
        matched = same_key and sim >= SIM_THRESHOLD
        ok = matched == should_match
        failures += not ok
        verdict = "מאוחד" if matched else "נפרד"
        print(f"  {'✓' if ok else '✗'} {verdict:<7} דמיון={sim:.2f}  {a[:36]}")

    print("\n── העשרה מלאה " + "─" * 47)
    sample = "🔴 דחוף | דיווח על נפילת רקטה בשדרות, כוחות בדרך למקום. @channel"
    result = enrich(sample)
    for key in ("title", "severity", "location_name", "latitude", "lang"):
        print(f"  {key:<15} {result[key]}")
    assert "@channel" not in result["content"], "boilerplate לא נוקה"
    assert "🔴" not in result["content"], "אימוג'י לא נוקה"
    print("  ניקוי           ✓ אימוג'י ו-@handle הוסרו")

    print("\n" + ("✅ הכל עבר" if not failures else f"❌ {failures} כשלים"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
