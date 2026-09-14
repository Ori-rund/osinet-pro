# חיבור טלגרם · מדריך

השלב היחיד שדורש פעולה ידנית חד-פעמית: התחברות לחשבון טלגרם
כדי לקבל "מחרוזת session". אחריה הכל אוטומטי.

## למה זה בכלל צריך חשבון

בוט טלגרם רואה רק ערוצים שהוא מנהל בהם. לקריאת ערוץ ציבורי
שאינו שלך נדרש חשבון משתמש רגיל — וזה פרוטוקול MTProto,
לא Bot API. לכן צריך התחברות אמיתית עם מספר טלפון.

**המלצה: מספר נפרד, לא האישי שלך.** חשבון שמצטרף להרבה ערוצים
במכה עלול לחטוף הגבלת ספאם מטלגרם.

---

## שלב 1 · מפתחות API

1. להיכנס ל-https://my.telegram.org
2. להתחבר עם מספר הטלפון (קוד יגיע בטלגרם עצמו)
3. **API development tools**
4. למלא טופס קצר — App title ו-Short name, כל שם יעבוד
5. לשמור את `api_id` ואת `api_hash`

## שלב 2 · מחרוזת session

צריך להריץ פייתון פעם אחת. שתי דרכים.

### דרך א · Google Colab — בלי להתקין כלום

1. https://colab.research.google.com → **New notebook**
2. להדביק בתא ולהריץ (כפתור ▶):

```python
!pip install -q telethon
from telethon import TelegramClient
from telethon.sessions import StringSession
import getpass

api_id   = int(input("api_id: "))
api_hash = getpass.getpass("api_hash: ")

client = TelegramClient(StringSession(), api_id, api_hash)
await client.connect()
phone = input("טלפון (למשל +9725...): ")
await client.send_code_request(phone)
code = input("הקוד שהגיע בטלגרם: ")
try:
    await client.sign_in(phone, code)
except Exception:
    await client.sign_in(password=getpass.getpass("סיסמת דו-שלבי: "))

print("\nTELEGRAM_SESSION=" + client.session.save())
```

3. להעתיק את המחרוזת הארוכה שמודפסת
4. **למחוק את הנוטבוק** — הפלט מכיל גישה מלאה לחשבון

### דרך ב · על המחשב שלך

דורש פייתון מותקן:

```bash
pip install telethon python-dotenv
python login_telegram.py
```

---

## שלב 3 · Railway

לשונית **Variables**, שלושה משתנים חדשים:

| שם | ערך |
|---|---|
| `TELEGRAM_API_ID` | מ-my.telegram.org |
| `TELEGRAM_API_HASH` | מ-my.telegram.org |
| `TELEGRAM_SESSION` | המחרוזת משלב 2 |

⚠️ **המחרוזת היא גישה מלאה לחשבון.** לא ב-git, לא בצ'אט, לא
בצילום מסך. אם דלפה: טלגרם → Settings → Devices → Terminate.

## שלב 4 · הוספת ערוצים

בממשק האתר (ניהול מקורות), או ב-SQL:

```sql
insert into sources (name, url, source_type, is_active, priority, trust_score)
values
  ('שם הערוץ', 'https://t.me/channel_name', 'telegram', true, 85, 60);
```

`source_type` חייב להיות בדיוק `telegram`.

---

## מה יקרה אחרי הפריסה

בלוגים של Railway אמור להופיע:

```
טלגרם מחובר כ-<שם>
מאזין ל-N ערוצים
backfill · <ערוץ> · {'inserted': 8, ...}
```

הודעות חדשות יגיעו לאתר תוך שניות — הן עוברות את אותה שכבת
סינון כמו RSS, כולל חוקי promote ו-suppress.

## תקלות נפוצות

| בלוג | מה לעשות |
|---|---|
| `טלגרם לא מוגדר` | חסר אחד משלושת המשתנים ב-Railway |
| `ערוץ לא נגיש` | הערוץ פרטי או שהשם שגוי. לבדוק שהקישור נפתח בדפדפן אנונימי |
| `FloodWait` | טלגרם מגביל קצב. הקוד ממתין לבד — לא לגעת |
| `AuthKeyUnregistered` | ה-session בוטל. להריץ שוב שלב 2 |
