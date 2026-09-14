-- OSINET-PRO · מיגרציה 001 · שכבת קליטה
-- בטוח להרצה חוזרת (idempotent). להריץ ב-Supabase → SQL Editor.

-- ─────────────────────────────────────────────────────────────
-- 1. הרחבת טבלת sources: מה ה-collector צריך כדי לעבוד
-- ─────────────────────────────────────────────────────────────
alter table public.sources add column if not exists handle             text;
alter table public.sources add column if not exists fetch_interval_sec integer not null default 300;
alter table public.sources add column if not exists last_fetched_at    timestamptz;
alter table public.sources add column if not exists last_success_at    timestamptz;
alter table public.sources add column if not exists last_error         text;
alter table public.sources add column if not exists error_count        integer not null default 0;
alter table public.sources add column if not exists default_severity   text default 'low';
alter table public.sources add column if not exists trust_score        integer not null default 50;

comment on column public.sources.handle is
  'מזהה טכני: שם ערוץ טלגרם ללא @ , או כתובת פיד RSS. נגזר מ-url אם ריק.';
comment on column public.sources.trust_score is
  '0-100. משמש לבחירת המקור הראשי כשכמה מקורות מדווחים על אותו אירוע.';

-- ─────────────────────────────────────────────────────────────
-- 2. הרחבת טבלת reports: קישור למקור + דה-דופליקציה
-- ─────────────────────────────────────────────────────────────
alter table public.reports add column if not exists source_id     uuid references public.sources(id) on delete set null;
alter table public.reports add column if not exists external_id   text;
alter table public.reports add column if not exists dedup_key     text;
alter table public.reports add column if not exists published_at  timestamptz;
alter table public.reports add column if not exists ingested_at   timestamptz not null default now();
alter table public.reports add column if not exists source_count  integer not null default 1;
alter table public.reports add column if not exists lang          text;
alter table public.reports add column if not exists raw           jsonb;

comment on column public.reports.external_id is
  'מזהה ההודעה במקור: message_id בטלגרם, guid/link ב-RSS. מונע קליטה כפולה.';
comment on column public.reports.dedup_key is
  'טביעת אצבע של התוכן. אותו אירוע מכמה מקורות → אותו dedup_key.';
comment on column public.reports.source_count is
  'כמה מקורות שונים דיווחו על האירוע הזה. 1 = בלעדי.';

-- מונע קליטה כפולה של אותה הודעה מאותו מקור. זה הבולם הראשי.
create unique index if not exists reports_source_external_uniq
  on public.reports (source_id, external_id)
  where external_id is not null;

create index if not exists reports_dedup_key_idx    on public.reports (dedup_key, published_at desc);
create index if not exists reports_published_at_idx on public.reports (published_at desc);
create index if not exists reports_geo_idx          on public.reports (latitude, longitude)
  where latitude is not null;

-- ─────────────────────────────────────────────────────────────
-- 3. report_sources: אירוע אחד, כמה מקורות שדיווחו עליו
--    זה מה שהופך "8 ערוצים צעקו אותו דבר" לכרטיס אחד עם 8 אסמכתאות.
-- ─────────────────────────────────────────────────────────────
create table if not exists public.report_sources (
  id           uuid primary key default gen_random_uuid(),
  report_id    uuid not null references public.reports(id) on delete cascade,
  source_id    uuid          references public.sources(id) on delete set null,
  source_name  text,
  source_url   text,
  external_id  text,
  published_at timestamptz,
  excerpt      text,
  created_at   timestamptz not null default now()
);

create unique index if not exists report_sources_uniq
  on public.report_sources (report_id, source_id, external_id);
create index if not exists report_sources_report_idx on public.report_sources (report_id);

-- ─────────────────────────────────────────────────────────────
-- 4. ingest_log: למה מקור הפסיק לעבוד. בלי זה מנפים באפלה.
-- ─────────────────────────────────────────────────────────────
create table if not exists public.ingest_log (
  id          bigserial primary key,
  source_id   uuid references public.sources(id) on delete cascade,
  run_at      timestamptz not null default now(),
  ok          boolean not null,
  fetched     integer not null default 0,
  inserted    integer not null default 0,
  deduped     integer not null default 0,
  duration_ms integer,
  error       text
);

create index if not exists ingest_log_source_run_idx on public.ingest_log (source_id, run_at desc);

-- ─────────────────────────────────────────────────────────────
-- 5. RLS · הפרונט קורא עם המפתח הציבורי, אז הכל נשען על זה.
--    ה-collector עובד עם service_role ועוקף RLS — זה בכוונה.
-- ─────────────────────────────────────────────────────────────
alter table public.report_sources enable row level security;
alter table public.ingest_log     enable row level security;

drop policy if exists report_sources_read on public.report_sources;
create policy report_sources_read on public.report_sources
  for select using (public.has_access() or public.is_admin());

drop policy if exists ingest_log_admin_read on public.ingest_log;
create policy ingest_log_admin_read on public.ingest_log
  for select using (public.is_admin());

-- ─────────────────────────────────────────────────────────────
-- 6. מילוי לאחור: לקשר דיווחים קיימים למקורות לפי שם
-- ─────────────────────────────────────────────────────────────
update public.reports r
   set source_id = s.id
  from public.sources s
 where r.source_id is null
   and r.source_name is not null
   and s.name = r.source_name;

update public.reports
   set published_at = created_at
 where published_at is null;

update public.sources
   set handle = regexp_replace(url, '^https?://(t\.me|telegram\.me)/(s/)?', '')
 where handle is null and url ~ '^https?://(t\.me|telegram\.me)/';

update public.sources
   set handle = url
 where handle is null;
