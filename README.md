# OSINET-PRO · Collector

הוורקר שמזרים דאטה אמיתי לאתר. רץ בנפרד מ-Vercel, כותב ל-Supabase,
והאתר מתעדכן לבד דרך Realtime שכבר מחובר ב-`index.html`.

```
ערוצי טלגרם ──┐
              ├──> collector ──> Supabase (reports) ──> Realtime ──> האתר
פידי RSS ─────┘         │
                        └── ניקוי · מיקום · חומרה · דה-דופליקציה
```

## למה לא על Vercel

פונקציות Vercel הן serverless — הן מתות אחרי שניות. Telethon צריך
חיבור MTProto מתמשך וקובץ session שנשמר בין הרצות. זה לא מתיישב,
ולכן הוורקר רץ כתהליך נפרד (Railway / Fly / כל VPS).

---

## הרצה מקומית

```bash
cd collector
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # ומלא את SUPABASE_SERVICE_KEY

python test_enrich.py    # בדיקות ההעשרה — צריך לעבור לפני הכל
DRY_RUN=true python main.py rss   # סבב RSS שמדפיס ולא כותב
```

`DRY_RUN=true` הוא הדרך לבדוק מקור חדש בלי ללכלך את ה-DB. תמיד
להתחיל ממנו כשמוסיפים ערוץ.

---

## הפעלה — לפי הסדר

### 1. מיגרציה

Supabase → SQL Editor → להריץ את `sql/001_ingest_layer.sql`.
בטוח להרצה חוזרת.

### 2. מפתח service_role

Supabase → Project Settings → API → `service_role` secret.
זה עוקף RLS — רק כמשתנה סביבה בוורקר, לעולם לא בצד לקוח ולא ב-git.

### 3. session לטלגרם

```bash
python login_telegram.py
```

מריצים **פעם אחת מקומית**. מזינים טלפון + הקוד שמגיע בטלגרם,
ומקבלים מחרוזת ארוכה → `TELEGRAM_SESSION`.

המחרוזת היא גישה מלאה לחשבון. אם דלפה:
Telegram → Settings → Devices → Terminate.

### 4. הוספת מקורות

בממשק האתר (ניהול מקורות) או ישירות:

```sql
insert into sources (name, url, source_type, is_active, priority, trust_score)
values
  ('Ynet מבזקים', 'https://www.ynet.co.il/Integration/StoryRss2.xml', 'rss', true, 80, 75),
  ('ערוץ חדשות', 'https://t.me/some_channel',                         'telegram', true, 70, 55);
```

`source_type` חייב להיות `rss` או `telegram` — הוורקר מנתב לפיו.

### 5. פריסה ב-Railway

New Project → Deploy from GitHub → תיקיית `collector`.
יש `Dockerfile`, אז Railway מזהה לבד. משתני סביבה: מה שב-`.env.example`.

חשוב: זה **Worker**, לא Web Service. אין פורט ואין healthcheck.

---

## מה קורה לכל הודעה

| שלב | קובץ | מה |
|---|---|---|
| ניקוי | `enrich.clean_text` | ניקוד, אימוג'י, קישורים, `@handle`, ספאם חוזר |
| מיקום | `enrich.extract_location` | גזטיר עברי → `latitude`/`longitude` → סיכה במפה |
| חומרה | `enrich.classify_severity` | מילות מפתח → `critical`/`high`/`medium`/`low` |
| חסימה | `enrich.dedup_key` | מקום + חלון 3 שעות → קבוצת מועמדים |
| הכרעה | `store.find_duplicate` | Jaccard ≥ 0.35 → אירוע קיים או חדש |
| כתיבה | `store.save` | `reports` + `report_sources` |

### על הדה-דופליקציה

הגרסה הראשונה חישבה טביעת אצבע מכל מילות ההודעה. זה נכשל: כל
הבדל ניסוח שינה את המפתח, ושני דיווחים על אותו אירוע לא נפגשו
לעולם. המבנה הנוכחי מפריד בין השלבים —

**מפתח חסימה** גס (מקום + חלון זמן) מביא מועמדים ברוחב,
**מדד דמיון** מכריע בדיוק. זה גם מה שמאפשר לכוונן: `SIMILARITY_THRESHOLD`
ב-`store.py` הוא הבורג היחיד. 0.35 כוונן על טקסטים עבריים — אותו אירוע
בניסוח שונה מקבל ~0.50, שני אירועים באותו יישוב מקבלים ~0.15.

כשמזוהה כפילות, `source_count` עולה ורשומה נוספת נכנסת ל-`report_sources`.
שמונה ערוצים על אותו אירוע = כרטיס אחד עם שמונה אסמכתאות. זה גם
אות אמינות: `source_count` גבוה = אירוע מאומת.

### אותיות שימוש — הדבר שהכי קל לפספס

בעברית ב/ל/מ/ה/ו/כ/ש נדבקות למילה. `"בקריית שמונה"` **לא מכיל**
את המחרוזת `"קריית שמונה"`, ולכן חיפוש תמים מחזיר אפס התאמות
בשקט. `_term_pattern` ו-`strip_prefix` מטפלים בזה. אם תוסיף לוגיקת
טקסט חדשה — זו הבדיקה הראשונה שצריך לכתוב.

---

## תחזוקה

```sql
-- מקורות שנפלו
select name, last_error, last_success_at from sources
 where is_active and (last_error is not null
    or last_success_at < now() - interval '1 hour');

-- קצב קליטה ל-24 שעות
select s.name, count(*) fetches, sum(r.inserted) inserted, sum(r.deduped) deduped
  from ingest_log r join sources s on s.id = r.source_id
 where r.run_at > now() - interval '24 hours'
 group by s.name order by inserted desc;

-- האירועים הכי מאומתים היום
select title, source_count, severity from reports
 where published_at > now() - interval '24 hours'
 order by source_count desc limit 20;
```

`deduped` גבוה מ-`inserted` באופן קבוע → הסף נמוך מדי, אירועים
שונים מתאחדים. `deduped` תמיד 0 → הסף גבוה מדי, או ש-`published_at`
לא נשמר נכון והחלונות לא נפגשים.

---

## מגבלות ידועות

**סיווג מבוסס מילות מפתח** מכסה טוב את המקרה הרגיל ונשבר בדיוק
במקומות שחשובים: שלילה, ציטוט אירוע ישן, ואזכור שם יישוב ישראלי
בכתבה על משהו אחר. `enrich.classify_with_llm` היא נקודת ההרחבה —
קריאת API אחת עם פלט JSON מובנה וההיוריסטיקה כ-fallback.

**הגזטיר חלקי.** הוא מכסה ערים, אזורים וגבולות, לא כל יישוב.
כלל אצבע: שם שצף שלוש פעמים ולא נתפס — להוסיף ל-`gazetteer.py`.

**טלגרם ומגבלות קצב.** חשבון שמצטרף להרבה ערוצים במכה נתפס
כספאם. `JOIN_DELAY_SEC` קיים בשביל זה. עדיף מספר ייעודי ולא אישי.

**RSS — כותרת ותקציר בלבד.** `MAX_SUMMARY_CHARS=400` ותמיד עם
קישור למקור. שמירת כתבות מלאות בכלי ארגוני היא חשיפה מיותרת.
