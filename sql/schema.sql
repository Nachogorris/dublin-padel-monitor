-- Dublin Padel Market Intelligence & Court Occupancy Tracker
-- SQLite 3 Schema (Europe/Dublin timezone-aware ISO-8601 timestamps)

PRAGMA foreign_keys = ON;

-- 1. Booking Platforms & Compliance Metadata
CREATE TABLE IF NOT EXISTS booking_platforms (
    platform_id             TEXT PRIMARY KEY,
    platform_name           TEXT NOT NULL,
    vendor_website          TEXT,
    official_api_status     TEXT NOT NULL, -- e.g. 'partner_or_club_tier_only', 'enterprise_partner_only', 'none_public'
    official_api_docs_url   TEXT,
    public_calendar_auth    TEXT NOT NULL, -- 'unauthenticated_web', 'login_required', 'cloudflare_gated', 'spa_internal_api', 'enquiry_only'
    robots_policy_summary   TEXT NOT NULL,
    terms_scraping_policy   TEXT NOT NULL,
    automated_slot_access   TEXT NOT NULL CHECK (automated_slot_access IN (
                                'permitted_public',
                                'authorized_api_only',
                                'blocked_robots_or_tos',
                                'blocked_auth_wall',
                                'blocked_antibot',
                                'no_online_calendar'
                            )),
    recommended_approach    TEXT NOT NULL,
    priority_rank           INTEGER,
    notes                   TEXT,
    updated_at              TEXT NOT NULL
);

-- 2. Verified Club Inventory
CREATE TABLE IF NOT EXISTS clubs (
    club_id                 TEXT PRIMARY KEY,
    club_name               TEXT NOT NULL,
    area                    TEXT NOT NULL,
    address                 TEXT,
    eircode                 TEXT,
    county                  TEXT NOT NULL DEFAULT 'Dublin',
    in_county_dublin        INTEGER NOT NULL DEFAULT 1 CHECK (in_county_dublin IN (0, 1)),
    inventory_cohort        TEXT NOT NULL CHECK (inventory_cohort IN (
                                'baseline_24_dublin',
                                'newly_verified_dublin',
                                'pipeline_planning_dublin',
                                'greater_dublin_border'
                            )),
    latitude                REAL,
    longitude               REAL,
    timezone                TEXT NOT NULL DEFAULT 'Europe/Dublin',
    website_url             TEXT,
    preliminary_courts      INTEGER,
    verified_padel_courts   INTEGER,
    doubles_courts          INTEGER,
    singles_courts          INTEGER,
    other_sport_courts      INTEGER DEFAULT 0,
    court_environment       TEXT NOT NULL CHECK (court_environment IN (
                                'indoor',
                                'covered_outdoor',
                                'outdoor',
                                'mixed',
                                'planned',
                                'unverified'
                            )),
    platform_id             TEXT REFERENCES booking_platforms(platform_id),
    platform_tenant_id      TEXT,
    public_booking_url      TEXT,
    access_model            TEXT NOT NULL CHECK (access_model IN (
                                'public_pay_and_play',
                                'hybrid_member_visitor',
                                'members_only',
                                'planned_not_open'
                            )),
    unauthenticated_calendar_visible INTEGER NOT NULL DEFAULT 0 CHECK (unauthenticated_calendar_visible IN (0, 1)),
    automated_collection_status TEXT NOT NULL CHECK (automated_collection_status IN (
                                'live_metadata_monitored_slots_need_api',
                                'live_slots_monitored',
                                'technically_accessible_not_integrated',
                                'awaiting_permission_or_api_access',
                                'not_accessible_auth_or_antibot',
                                'not_accessible_members_only',
                                'not_accessible_no_online_calendar',
                                'planned_venue'
                            )),
    off_peak_price_eur      REAL,
    peak_price_eur          REAL,
    price_notes             TEXT,
    opening_hours_summary   TEXT,
    verification_confidence TEXT NOT NULL CHECK (verification_confidence IN ('high', 'medium', 'low')),
    verification_sources    TEXT,
    verification_notes      TEXT,
    updated_at              TEXT NOT NULL
);

-- 3. Individual Courts
CREATE TABLE IF NOT EXISTS courts (
    club_id                 TEXT NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    court_id                TEXT NOT NULL,
    court_name              TEXT NOT NULL,
    sport                   TEXT NOT NULL DEFAULT 'PADEL',
    court_environment       TEXT NOT NULL CHECK (court_environment IN ('indoor', 'covered_outdoor', 'outdoor', 'unknown')),
    court_size              TEXT NOT NULL CHECK (court_size IN ('double', 'single', 'unknown')),
    wall_type               TEXT,
    is_active               INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    source_origin           TEXT NOT NULL CHECK (source_origin IN ('live_public', 'live_authorized_api', 'directory_inferred', 'synthetic_validation')),
    verified_at             TEXT NOT NULL,
    PRIMARY KEY (club_id, court_id)
);

-- 4. Structured Weekly Opening Hours (Local Europe/Dublin Wall Clock)
CREATE TABLE IF NOT EXISTS club_opening_hours (
    club_id                 TEXT NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    day_of_week             TEXT NOT NULL CHECK (day_of_week IN (
                                'MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY', 'HOLIDAYS'
                            )),
    opening_time            TEXT NOT NULL, -- HH:MM in Europe/Dublin (e.g. '06:00')
    closing_time            TEXT NOT NULL, -- HH:MM in Europe/Dublin; values <= opening_time (e.g. '00:30', '01:00') mean next calendar day
    closes_next_day         INTEGER NOT NULL DEFAULT 0 CHECK (closes_next_day IN (0, 1)),
    source_origin           TEXT NOT NULL DEFAULT 'live_public',
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (club_id, day_of_week)
);

-- 5. Collection Run Audit Log (Every live test, probe, ingestion, or synthetic load)
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id                  TEXT PRIMARY KEY,
    started_at              TEXT NOT NULL,
    completed_at            TEXT,
    adapter_name            TEXT NOT NULL,
    platform_id             TEXT,
    club_id                 TEXT REFERENCES clubs(club_id),
    target_url              TEXT,
    http_status             INTEGER,
    robots_allowed          INTEGER CHECK (robots_allowed IN (0, 1)),
    auth_required           INTEGER CHECK (auth_required IN (0, 1)),
    run_status              TEXT NOT NULL CHECK (run_status IN (
                                'success',
                                'blocked_compliance',
                                'blocked_auth',
                                'blocked_antibot',
                                'http_error',
                                'parse_error'
                            )),
    data_origin             TEXT NOT NULL CHECK (data_origin IN ('live_public', 'live_authorized_api', 'synthetic_validation')),
    records_written         INTEGER NOT NULL DEFAULT 0,
    error_message           TEXT,
    raw_artifact_path       TEXT
);

-- 6. Publicly Scheduled Club Activities (Tournaments, Americanos, Academy Courses)
CREATE TABLE IF NOT EXISTS public_club_events (
    club_id                 TEXT NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    event_id                TEXT NOT NULL,
    event_type              TEXT NOT NULL CHECK (event_type IN ('competition', 'academy_course', 'public_class', 'other')),
    title                   TEXT,
    start_time              TEXT, -- ISO-8601 with tz offset
    price_eur               REAL,
    source_url              TEXT NOT NULL,
    observed_at             TEXT NOT NULL,
    run_id                  TEXT REFERENCES collection_runs(run_id),
    PRIMARY KEY (club_id, event_id)
);

-- 7. Slot Availability Observations (Append-Only Historical Snapshots)
CREATE TABLE IF NOT EXISTS availability_observations (
    observation_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id                 TEXT NOT NULL REFERENCES clubs(club_id),
    court_id                TEXT NOT NULL,
    booking_platform        TEXT NOT NULL,
    target_date             TEXT NOT NULL, -- YYYY-MM-DD in Europe/Dublin
    start_time              TEXT NOT NULL, -- ISO-8601 with Europe/Dublin offset
    end_time                TEXT NOT NULL, -- ISO-8601 with Europe/Dublin offset
    duration_minutes        INTEGER NOT NULL CHECK (duration_minutes > 0),
    price_eur               REAL CHECK (price_eur IS NULL OR price_eur >= 0),
    availability_status     TEXT NOT NULL CHECK (availability_status IN (
                                'available',
                                'unavailable',
                                'blocked',
                                'unknown'
                            )),
    status_detail           TEXT NOT NULL DEFAULT 'unspecified' CHECK (status_detail IN (
                                'confirmed_booking',
                                'observed_available',
                                'inferred_unavailable',
                                'observed_unavailable',
                                'blocked_maintenance',
                                'blocked_coaching_or_event',
                                'missing_unknown',
                                'unspecified'
                            )),
    observed_at             TEXT NOT NULL, -- ISO-8601 with offset
    lead_time_hours         REAL,
    snapshot_bucket         TEXT NOT NULL DEFAULT 'ad_hoc' CHECK (snapshot_bucket IN (
                                '7d', '3d', '24h', '6h', '1h', 'ad_hoc'
                            )),
    source_url              TEXT NOT NULL,
    data_origin             TEXT NOT NULL CHECK (data_origin IN (
                                'live_public',
                                'live_authorized_api',
                                'synthetic_validation'
                            )),
    run_id                  TEXT REFERENCES collection_runs(run_id),
    FOREIGN KEY (club_id, court_id) REFERENCES courts(club_id, court_id),
    UNIQUE (club_id, court_id, start_time, end_time, observed_at, data_origin)
);

CREATE INDEX IF NOT EXISTS idx_obs_club_date_origin
    ON availability_observations (data_origin, club_id, target_date);

CREATE INDEX IF NOT EXISTS idx_obs_slot_lookup
    ON availability_observations (data_origin, club_id, court_id, start_time, observed_at);

CREATE INDEX IF NOT EXISTS idx_obs_bucket
    ON availability_observations (data_origin, snapshot_bucket, availability_status);

-- 7b. Stateful Court Booking Ledger (Cumulative Master State Machine by 30-min Slot)
CREATE TABLE IF NOT EXISTS court_slot_ledger (
    slot_id                 TEXT PRIMARY KEY, -- e.g. "club_id:court_id:YYYY-MM-DD:HH:MM"
    club_id                 TEXT NOT NULL REFERENCES clubs(club_id),
    court_id                TEXT NOT NULL,
    target_date             TEXT NOT NULL, -- YYYY-MM-DD in Europe/Dublin
    start_time              TEXT NOT NULL, -- ISO-8601 with tz offset
    end_time                TEXT NOT NULL, -- ISO-8601 with tz offset
    slot_time               TEXT NOT NULL, -- HH:MM (e.g. "13:30")
    booking_status          TEXT NOT NULL CHECK (booking_status IN ('available', 'booked', 'maintenance', 'unbooked_passed')),
    first_detected_at       TEXT,          -- Timestamp of snapshot when first seen booked
    lead_time_hours         REAL,          -- Hours in advance when booking was first detected
    price_eur               REAL,          -- Actual or verified rental price for this 30m slot in EUR
    last_snapshot_at        TEXT NOT NULL, -- Latest snapshot timestamp that verified this slot
    snapshots_seen_count    INTEGER DEFAULT 1,
    data_origin             TEXT NOT NULL DEFAULT 'live_public',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    FOREIGN KEY (club_id, court_id) REFERENCES courts(club_id, court_id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_club_date ON court_slot_ledger(club_id, target_date);
CREATE INDEX IF NOT EXISTS idx_ledger_date ON court_slot_ledger(target_date);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON court_slot_ledger(booking_status);


-- 8. Analytical View: Club Coverage Summary
CREATE VIEW IF NOT EXISTS v_county_dublin_coverage AS
SELECT
    c.automated_collection_status,
    c.platform_id,
    COUNT(*) AS club_count,
    SUM(COALESCE(c.verified_padel_courts, c.preliminary_courts, 0)) AS padel_courts
FROM clubs c
WHERE c.in_county_dublin = 1
  AND c.inventory_cohort IN ('baseline_24_dublin', 'newly_verified_dublin')
GROUP BY c.automated_collection_status, c.platform_id
ORDER BY padel_courts DESC;

-- 9. API-Free Live Activity, Court-Block & Player Fill-Rate Telemetry (Append-Only Time Series)
CREATE TABLE IF NOT EXISTS activity_telemetry_snapshots (
    telemetry_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id                     TEXT NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    activity_id                 TEXT NOT NULL,
    activity_category           TEXT NOT NULL CHECK (activity_category IN (
                                    'tournament', 'public_class', 'academy_course'
                                )),
    title                       TEXT NOT NULL,
    start_time                  TEXT,
    end_time                    TEXT,
    duration_minutes            INTEGER,
    course_span_text            TEXT,
    skill_band                  TEXT,
    coach_name                  TEXT,
    registered_players          INTEGER,
    max_players                 INTEGER,
    fill_rate_pct               REAL,
    courts_occupied             INTEGER NOT NULL DEFAULT 1,
    court_hours_occupied        REAL NOT NULL DEFAULT 0.0,
    price_per_player_eur        REAL,
    observed_gross_revenue_eur  REAL,
    full_capacity_revenue_eur   REAL,
    observed_at                 TEXT NOT NULL,
    source_url                  TEXT NOT NULL,
    run_id                      TEXT REFERENCES collection_runs(run_id),
    UNIQUE (club_id, activity_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_activity_telemetry_lookup
    ON activity_telemetry_snapshots (club_id, activity_id, observed_at);

-- 10. API-Free Club Membership & Lesson Bundle Economic Signals
CREATE TABLE IF NOT EXISTS club_membership_offers (
    club_id                     TEXT NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    offer_id                    TEXT NOT NULL,
    offer_kind                  TEXT NOT NULL CHECK (offer_kind IN (
                                    'membership', 'lesson_package', 'wallet_bundle'
                                )),
    title                       TEXT NOT NULL,
    price_eur                   REAL,
    billing_period              TEXT,
    court_discount_pct          REAL,
    advance_booking_days        INTEGER,
    off_peak_unlimited          INTEGER NOT NULL DEFAULT 0 CHECK (off_peak_unlimited IN (0, 1)),
    description                 TEXT,
    observed_at                 TEXT NOT NULL,
    PRIMARY KEY (club_id, offer_id)
);

-- 11. Zero-API Human-in-the-Loop Spot Checks (Browser Visual / Phone / On-Site Counts)
CREATE TABLE IF NOT EXISTS field_spot_checks (
    spot_check_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id                     TEXT NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    observed_at                 TEXT NOT NULL,
    target_slot_start           TEXT NOT NULL,
    target_slot_end             TEXT NOT NULL,
    time_stratum                TEXT NOT NULL CHECK (time_stratum IN (
                                    'weekday_offpeak', 'weekday_peak', 'weekend_prime', 'weekend_offpeak'
                                )),
    total_courts_checked        INTEGER NOT NULL CHECK (total_courts_checked > 0),
    occupied_courts             INTEGER NOT NULL CHECK (occupied_courts >= 0),
    blocked_courts              INTEGER NOT NULL DEFAULT 0 CHECK (blocked_courts >= 0),
    available_courts            INTEGER NOT NULL CHECK (available_courts >= 0),
    displayed_price_eur         REAL,
    collection_method           TEXT NOT NULL CHECK (collection_method IN (
                                    'browser_visual_check', 'phone_inquiry', 'onsite_headcount', 'browser_dom_inbox'
                                )),
    observer_notes              TEXT,
    UNIQUE (club_id, target_slot_start, observed_at, collection_method)
);

-- 12. API-Free Multi-Signal Weekly Court Utilization & Revenue Measurements
CREATE TABLE IF NOT EXISTS non_api_club_measurements (
    club_id                             TEXT PRIMARY KEY REFERENCES clubs(club_id) ON DELETE CASCADE,
    reference_week_start                TEXT NOT NULL,
    verified_padel_courts               INTEGER NOT NULL,
    weekly_capacity_court_hours         REAL NOT NULL,
    direct_organised_court_hours        REAL NOT NULL,
    live_activities_count               INTEGER NOT NULL,
    live_registered_players             INTEGER NOT NULL,
    live_max_player_spots               INTEGER NOT NULL,
    live_activity_fill_rate_pct         REAL,
    player_signup_24h_delta             INTEGER,
    verified_activity_revenue_eur       REAL NOT NULL,
    spot_check_samples                  INTEGER NOT NULL DEFAULT 0,
    estimated_casual_court_hours_point  REAL NOT NULL,
    total_occupied_court_hours_low      REAL NOT NULL,
    total_occupied_court_hours_point    REAL NOT NULL,
    total_occupied_court_hours_high     REAL NOT NULL,
    estimated_occupancy_pct_low         REAL NOT NULL,
    estimated_occupancy_pct_point       REAL NOT NULL,
    estimated_occupancy_pct_high        REAL NOT NULL,
    peak_occupancy_pct_point            REAL NOT NULL,
    offpeak_occupancy_pct_point         REAL NOT NULL,
    est_weekly_gbv_eur_low              REAL NOT NULL,
    est_weekly_gbv_eur_point            REAL NOT NULL,
    est_weekly_gbv_eur_high             REAL NOT NULL,
    measurement_tier                    TEXT NOT NULL,
    confidence_level                    TEXT NOT NULL,
    confidence_score                    INTEGER NOT NULL,
    methodology_notes                   TEXT NOT NULL,
    computed_at                         TEXT NOT NULL
);

