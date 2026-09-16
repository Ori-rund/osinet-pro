"""בדיקות לשכבת ההעשרה. הרצה: python test_enrich.py"""

from enrich import classify_severity, dedup_key, enrich, extract_location, significant_overlap, similarity

GEO_CASES = [
    ("נשמעו אזעקות בקריית שמונה ובסביבתה", "קריית שמונה"),
    ("דיווח על ירי לעבר שדרות, פיקוד העורף בבדיקה", "שדרות"),
    ("פיגוע דקירה בעיר העתיקה בירושלים", "העיר העתיקה"),
    ("תקיפה בדרום לבנון לפי דיווחים ערביים", "דרום לבנון"),
    ("עומסי תנועה בכביש 6 צפונה", "כביש 6"),
    ("מזג האוויר מחר יהיה נעים", None),
    ("התרחשות ברמת הגולן סמוך לגבול", "רמת הגולן"),
    ('אירוע ביטחוני בת"א', "תל אביב"),
    ("הצתת כלי רכב בעין כרם", "עין כרם"),
    # "מודיעין"/"רימונים" כמילים כלליות — לא שמות המקומות באותו שם
    ("איסוף מודיעין במכון ויצמן", None),
    ("נמצאו רימונים וכלי נשק בדירה", None),
    # שם מלא וחד-משמעי — כן ממשיך לעבוד למרות ה"מודיעין" הבודד למעלה
    ("אזעקה במודיעין מכבים רעות", "מודיעין"),
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


# מקרה אמיתי: האתר הראה 0 אירועים מאומתים מתוך 121 דיווחים.
# ארבעה אתרים סיקרו אותו אירוע ואף אחד לא התאחה. הבדיקה הזו
# מקבעת את שני הכיוונים — שאיחוד קורה, ושהוא לא קורה מדי.
MERGE_BASE = "אזעקות הופעלו בקריית שמונה ובגליל העליון, אין נפגעים"
SHOULD_MERGE = [
    "פיקוד העורף הפעיל אזעקות בקריית שמונה; לא דווח על נפגעים",
    "אזעקה בקריית שמונה: תושבים נכנסו למרחב מוגן",
    "דיווח על אזעקות באזור קריית שמונה והגליל",
]
SHOULD_NOT_MERGE = [
    "תאונת דרכים בכניסה לקריית שמונה, שני פצועים",
    "שריפה פרצה במבנה בקריית שמונה, אין נפגעים",
    "נעצר חשוד בקריית שמונה על רקע פלילי",
    "הפגנה חסמה את הכביש בקריית שמונה",
]
MERGE_THRESHOLD = 0.30

# מקרה אמיתי שדלף מהפיד: שלושה ערוצי טלגרם על אותה פשיטה בכפר יטא,
# בניסוח כה שונה (אחד יבש, אחד "סצנת לחימה מסרט הוליוודי") שקיבלו
# Jaccard של 0.116 — נמוך יותר משני אירועים שונים באותו יישוב. זה
# בדיוק המקרה ש-store.find_duplicate מטפל בו דרך significant_overlap
# ולא דרך סף Jaccard (ראה שם TIGHT_WINDOW_MINUTES).
OVERLAP_CASES = [
    ('פעילות מיוחדת בעיירה יטא | דיווחים ערביים: כוחות צה"ל פועלים בשעה זו '
     'בעיירה יטא שבמרחב חברון, במסגרת הפעילות כוחות מיוחדים הוצנחו ממסוק '
     'על גג מבנה - פרטים נוספים בהמשך.',
     'תיעוד חריג בטירוף - כח מיוחד של צה"ל פורץ לבית מבוקש בכפר יטא שבנפת '
     'חברון באמצעות השתלשלות ממסוק קרב! על פי הדיווחים הערביים לאחר שהכוחות '
     'נכנסו לבית זרמו למקום רכבים משוריינים של הצבא וסגרו את כל הכניסות '
     'והיציאות לכפר.',
     "חברון", True, "אותה פשיטה ביטא, ניסוח שונה לגמרי"),
    (MERGE_BASE, SHOULD_MERGE[0], "קריית שמונה", True, "אותו אירוע"),
    (MERGE_BASE, SHOULD_NOT_MERGE[0], "קריית שמונה", False, "תאונת דרכים — לא קשור"),
    (MERGE_BASE, SHOULD_NOT_MERGE[1], "קריית שמונה", False, "שריפה — לא קשור"),
]


def check_merging() -> int:
    """מוודא שהאיחוד עובד בשני הכיוונים, עם מרווח בין הקבוצות."""
    failures = 0
    base = enrich(MERGE_BASE)
    hits, misses = [], []

    print("\n── איחוד דיווחים · אותו אירוע " + "─" * 30)
    for text in SHOULD_MERGE:
        row = enrich(text)
        sim = similarity(base["content"], row["content"])
        merged = row["dedup_key"] == base["dedup_key"] and sim >= MERGE_THRESHOLD
        hits.append(sim)
        failures += not merged
        print(f"  {'✓' if merged else '✗'} {sim:.2f}  {text[:48]}")

    print("\n── איחוד דיווחים · אירועים שונים " + "─" * 27)
    for text in SHOULD_NOT_MERGE:
        row = enrich(text)
        sim = similarity(base["content"], row["content"])
        merged = sim >= MERGE_THRESHOLD
        misses.append(sim)
        failures += merged
        print(f"  {'✓' if not merged else '✗'} {sim:.2f}  {text[:48]}")

    gap = min(hits) - max(misses)
    print(f"\n  מרווח בין הקבוצות: {gap:+.2f} "
          f"(אמיתי {min(hits):.2f}-{max(hits):.2f} · שונה {min(misses):.2f}-{max(misses):.2f})")
    if gap <= 0.05:
        print("  ✗ המרווח צר מדי — הסף שביר")
        failures += 1
    return failures


def check_overlap() -> int:
    """המסלול המקל של find_duplicate: חפיפת מילים מעבר למיקום."""
    failures = 0
    print("\n── חפיפה מעבר למיקום (מסלול זמן קרוב) " + "─" * 15)
    for a, b, location, should_overlap, note in OVERLAP_CASES:
        overlap = significant_overlap(a, b, location)
        ok = bool(overlap) == should_overlap
        failures += not ok
        verdict = "חופף" if overlap else "ריק "
        print(f"  {'✓' if ok else '✗'} {verdict}  {sorted(overlap) or '—'}  ({note})")
    return failures


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

    ad = enrich("אזעקות בשדרות, אין נפגעים אפליקציית ׳אהרון ידיעות׳ רוצים לפרסם את העסק שלכם?")
    assert "רוצים לפרסם" not in ad["content"], "פרסומת ערוץ לא נוקתה"
    comments = enrich("תקיפה בעזה, שני הרוגים תגובה אחת")
    assert "תגובה" not in comments["content"], "מונה תגובות לא נוקה"
    comments2 = enrich("תקיפה בעזה, שני הרוגים 12 תגובות")
    assert "תגובות" not in comments2["content"], "מונה תגובות (מספר) לא נוקה"
    print("  ניקוי           ✓ פרסומת ערוץ ומונה תגובות הוסרו")

    failures += check_merging()
    failures += check_overlap()

    print("\n" + ("✅ הכל עבר" if not failures else f"❌ {failures} כשלים"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
