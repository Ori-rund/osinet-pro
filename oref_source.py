"""
קליטת התרעות פיקוד העורף (Home Front Command) בזמן אמת.

המקור הכי סמכותי שיש לאזעקות — לא צריך לחכות שערוץ טלגרם יעתיק
את זה, ולא צריך AI שישקול אם זה "אמיתי": אם פיקוד העורף פרסם
התרעה, זה אירוע. ה-API הציבורי (לא רשמי אבל זה שכל אפליקציות
ההתרעה משתמשות בו) מחזיר את מצב ההתרעות הפעילות *עכשיו*; גוף
ריק אומר "אין התרעה כרגע", לא שגיאה.

נקודת שוני קריטית מ-RSS/טלגרם: אותה התרעה חוזרת בתשובה בכל poll
כל עוד היא פעילה (זה snapshot של מצב, לא תור הודעות) — הדדופ
תלוי לגמרי ב-external_id היציב (שדה id של ה-API עצמו), אחרת אותה
התרעה נכנסת כדיווח חדש כל כמה שניות כל עוד היא נמשכת.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from enrich import classify_severity, clean_text, dedup_key
from gazetteer import ALIASES, PLACES
from store import active_sources, log_run, mark_fetched, save

log = logging.getLogger("oref")

ALERTS_URL = "https://www.oref.org.il/WarningMessages/alert/alerts.json"
# בלי Referer/X-Requested-With המקור חוסם את הבקשה — אלה בדיוק
# הכותרות ששולח הדפדפן כשהעמוד הרשמי מושך את ההתרעות בעצמו.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; osinet-pro/1.0; +https://osinet-pro.vercel.app)",
    "Referer": "https://www.oref.org.il/",
    "X-Requested-With": "XMLHttpRequest",
}


def _resolve_area(name: str) -> tuple[str, float | None, float | None]:
    """שם אזור רשמי מפיקוד העורף → (שם קנוני, lat, lng).

    שמות פיקוד העורף לא תמיד זהים אחד לאחד לגזטיר. ניסיון התאמה
    מדויקת (כולל ALIASES) קודם; בלי התאמה — עדיין מציגים את השם
    הגולמי בכרטיס, פשוט בלי נקודה על המפה.
    """
    name = (name or "").strip()
    canon = ALIASES.get(name, name)
    if canon in PLACES:
        lat, lng, _precision = PLACES[canon]
        return canon, lat, lng
    return name, None, None


def _parse_alerts(body: str) -> list[dict]:
    """גוף ריק / '{}' / לא-JSON = אין התרעה פעילה כרגע, לא שגיאה."""
    body = (body or "").strip()
    if not body or body == "{}":
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data] if data else []
    if not isinstance(data, list):
        return []
    return [a for a in data if isinstance(a, dict) and a.get("data")]


def fetch_once(source: dict) -> dict:
    """סבב poll אחד. לא זורק — כשל ברשת לא מפיל את הלולאה."""
    started = time.monotonic()
    counts = {"fetched": 0, "inserted": 0, "deduped": 0, "skipped": 0, "filtered": 0}

    try:
        response = httpx.get(ALERTS_URL, timeout=10.0, headers=HEADERS)
        response.raise_for_status()
        alerts = _parse_alerts(response.text)
    except Exception as exc:
        log.warning("קריאת פיקוד העורף נכשלה · %s", exc)
        mark_fetched(source["id"], ok=False, error=str(exc))
        log_run(source["id"], ok=False, error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000))
        return counts

    counts["fetched"] = len(alerts)
    published = datetime.now(timezone.utc).isoformat()

    for alert in alerts:
        areas = [a for a in (alert.get("data") or []) if a]
        if not areas:
            continue

        alert_title = clean_text(alert.get("title") or "התרעת פיקוד העורף")
        external_id = str(alert.get("id") or hashlib.sha1(
            f"{alert_title}|{'|'.join(areas)}".encode("utf-8")
        ).hexdigest()[:16])

        resolved = [_resolve_area(a) for a in areas[:12]]
        primary_name, primary_lat, primary_lng = resolved[0]
        area_names = ", ".join(a[0] for a in resolved)

        content = f"{alert_title}: {area_names}"
        desc = clean_text(alert.get("desc") or "")
        if desc:
            content += f". {desc}"

        title = f"{alert_title} — {primary_name}" if len(resolved) == 1 \
            else f"{alert_title}: {area_names[:70]}"

        candidate = {
            "title": title,
            "content": content,
            "severity": classify_severity(content, default="critical"),
            "status": "verified",
            "location_name": primary_name,
            "latitude": primary_lat,
            "longitude": primary_lng,
            "dedup_key": dedup_key(content, primary_name, None),
            "lang": "he",
            "source_id": source["id"],
            "source_name": source.get("name"),
            "source_type": "oref",
            "source_url": "https://www.oref.org.il/",
            "external_id": external_id,
            "published_at": published,
            "raw": {"areas": areas, "cat": alert.get("cat")},
        }

        # מקור רשמי — לא עובר דרך relevance.screen(). פיקוד העורף
        # הוא כבר הגורם הסמכותי לכך שזה אירוע אמיתי; אין כאן שיקול
        # דעת נוסף לעשות, לא AI ולא היוריסטיקה.
        outcome = save(candidate)
        counts[outcome] = counts.get(outcome, 0) + 1

    mark_fetched(source["id"], ok=True)
    log_run(source["id"], ok=True, fetched=counts["fetched"],
            inserted=counts["inserted"], deduped=counts["deduped"],
            duration_ms=int((time.monotonic() - started) * 1000))
    if counts["inserted"]:
        log.info("פיקוד העורף · התרעה חדשה · %d", counts["inserted"])
    return counts


def run_once() -> dict:
    """סבב אחד על מקור/ות ה-oref הפעילים (בפועל: אחד)."""
    sources = [s for s in active_sources()
               if (s.get("source_type") or "").lower() == "oref"]
    if not sources:
        return {}
    totals = {"fetched": 0, "inserted": 0, "deduped": 0, "skipped": 0, "filtered": 0}
    for source in sources:
        for key, value in fetch_once(source).items():
            totals[key] = totals.get(key, 0) + value
    return totals
