DO $$ BEGIN
    CREATE TYPE landing_mood AS ENUM ('link', 'mirror', 'local_file');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE status_mood AS ENUM ('pending', 'success', 'error');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE domain_error_handle_mood AS ENUM ('handle', 'error');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE campaign_type AS ENUM ('campaign', 'tracking_only');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE campaign_status AS ENUM ('active', 'paused');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE redirect_mode AS ENUM ('position', 'weight');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE ssl_status_mood AS ENUM ('not_started', 'pending', 'success', 'error');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE conversion_status AS ENUM ('lead', 'sale', 'upsale', 'rejected', 'hold', 'trash');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Create the users table
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    username VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT TRUE,
    -- G62/G63: kept in sync with models/user.py so a fresh database is complete
    -- even when the app's startup migrations run before the tables exist.
    totp_secret TEXT,
    totp_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    totp_backup JSONB,
    permissions JSONB
);





-- ===========================================================================
-- Multi-tenancy, phase 1: tenants + tenant membership + tenant_id backfill
-- ===========================================================================
-- Tenant #1 is this install. It is created before any tenant-owned table is
-- defined/populated so the DEFAULT 1 below always resolves. Depth support:
-- parent_tenant_id models one parent link (an agency and its sub-workspaces);
-- phase 1 does NOT walk the hierarchy — a membership in the parent gives no
-- access to the child (each tenant is an independent data plane).
CREATE TABLE IF NOT EXISTS tenants (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(255) NOT NULL UNIQUE,
    parent_tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    -- Phase 2A plan fields: plan/seats gate membership now, retention and
    -- feature flags drive later phases. seats NULL = unlimited.
    plan TEXT NOT NULL DEFAULT 'free',
    seats INTEGER,
    retention_days INTEGER,
    features JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS seats INTEGER;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS retention_days INTEGER;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS features JSONB NOT NULL DEFAULT '{}'::jsonb;

INSERT INTO tenants (id, name, slug) VALUES (1, 'Default', 'default')
ON CONFLICT DO NOTHING;
SELECT setval(pg_get_serial_sequence('tenants', 'id'),
              GREATEST((SELECT COALESCE(MAX(id), 1) FROM tenants), 1));

-- A user's role/permissions per tenant. users.permissions is copied here by
-- the phase-1 backfill; from phase 2A on, authority resolves from the
-- membership (role defaults layered under these explicit overrides).
CREATE TABLE IF NOT EXISTS tenant_memberships (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role VARCHAR(32) NOT NULL DEFAULT 'viewer',
    permissions JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (user_id, tenant_id)
);

-- tenant_id on every tenant-owned table, plus the composite uniques that used
-- to be global. Everything here is idempotent for installs that predate
-- multi-tenancy: add nullable -> backfill -> NOT NULL DEFAULT 1 (the "safe
-- fallback" the app's ORM hooks rely on). Every statement is guarded by
-- to_regclass so a *fresh* install (where this block runs before the tables
-- below exist) is a clean no-op and the CREATE TABLE statements own the shape.
-- No FK to tenants is added on these columns: the id-1 row exists, but a FK on
-- a hot insert path buys nothing — NOT NULL plus the application-level stamping
-- (backend/tenant_scope.py) is the enforcement.
DO $$
DECLARE
    t text;
    spec text;
    arr text[];
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'campaigns','domains','landings','affiliate_networks','offers','sources',
        'conversions_data','audit_log','capi_pixels','capi_pixel_bindings',
        'capi_channel_settings','capi_pixel_sent','meta_capi_sent','meta_capi_log',
        'ad_cost_daily','integration_connections','scripts','filter_presets',
        'funnel_templates','domain_groups','auto_rules','monitor_state',
        'honeypot_hits','postback_logs','click_forward_logs','cost_update_logs',
        'settings'
    ] LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id INTEGER', t);
            EXECUTE format('UPDATE %I SET tenant_id = 1 WHERE tenant_id IS NULL', t);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT 1', t);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET NOT NULL', t);
            EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (tenant_id)',
                           t || '_tenant_id_idx', t);
        END IF;
    END LOOP;

    -- {table}|{legacy global constraint}|{composite index}|{composite columns}
    FOREACH spec IN ARRAY ARRAY[
        'campaigns|campaigns_name_key|campaigns_tenant_name_key|name',
        'campaigns|campaigns_alias_key|campaigns_tenant_alias_key|alias',
        'domains|domains_domain_key|domains_tenant_domain_key|domain',
        'settings|settings_name_key|settings_tenant_name_key|name',
        'sources|sources_name_key|sources_tenant_name_key|name',
        'affiliate_networks|affiliate_networks_name_key|affiliate_networks_tenant_name_key|name',
        'offers|offers_name_key|offers_tenant_name_key|name',
        'landings|landings_folder_key|landings_tenant_folder_key|folder',
        'landings|landings_name_key|landings_tenant_name_key|name',
        'domain_groups|domain_groups_name_key|domain_groups_tenant_name_key|name',
        'integration_connections|integration_connections_platform_key|integration_connections_tenant_platform_key|platform'
    ] LOOP
        arr := string_to_array(spec, '|');
        IF to_regclass(arr[1]) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', arr[1], arr[2]);
            EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I (tenant_id, %s)',
                           arr[3], arr[1], arr[4]);
        END IF;
    END LOOP;

    -- ad_cost_daily keys on the day as well (backs the Meta Ads upsert).
    IF to_regclass('ad_cost_daily') IS NOT NULL THEN
        EXECUTE 'ALTER TABLE ad_cost_daily DROP CONSTRAINT IF EXISTS '
                'ad_cost_daily_platform_ad_account_id_platform_campaign_id_d_key';
        EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS ad_cost_daily_tenant_platform_key '
                'ON ad_cost_daily (tenant_id, platform, ad_account_id, '
                'platform_campaign_id, date)';
    END IF;

    -- {index}|{table}|{hot column} — composite indexes for tenant-scoped lookups.
    FOREACH spec IN ARRAY ARRAY[
        'conversions_tenant_received_idx|conversions_data|received_at',
        'conversions_tenant_campaign_idx|conversions_data|campaign_id',
        'conversions_tenant_click_id_idx|conversions_data|click_id',
        'click_forward_logs_tenant_created_idx|click_forward_logs|created_at',
        'postback_logs_tenant_received_idx|postback_logs|received_at',
        'audit_log_tenant_at_idx|audit_log|at'
    ] LOOP
        arr := string_to_array(spec, '|');
        IF to_regclass(arr[2]) IS NOT NULL THEN
            EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I (tenant_id, %I)',
                           arr[1], arr[2], arr[3]);
        END IF;
    END LOOP;
END $$;

-- Backfill: every existing user becomes a member of tenant 1 with the role
-- derived from is_admin, and their install-global permissions JSON copied onto
-- the membership so nobody's access changes. The lowest-id admin becomes
-- 'owner'; other admins 'admin'; everyone else 'editor'.
INSERT INTO tenant_memberships (user_id, tenant_id, role, permissions)
SELECT u.id, 1,
       CASE WHEN u.is_admin AND u.id = (SELECT MIN(id) FROM users WHERE is_admin)
                 THEN 'owner'
            WHEN u.is_admin THEN 'admin'
            ELSE 'editor' END,
       u.permissions
FROM users u
-- Only users with NO membership at all (a user provisioned into another
-- workspace later must not be silently added to tenant 1).
WHERE NOT EXISTS (SELECT 1 FROM tenant_memberships m
                  WHERE m.user_id = u.id);

CREATE TABLE IF NOT EXISTS domains (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(255) NOT NULL,                         -- domain address (unique per tenant)
    redirect_https BOOLEAN DEFAULT TRUE,                  -- whether to redirect to https
    handle_404 domain_error_handle_mood,                -- 'error' or 'redirect_to_company'
    default_campaign_id INTEGER,                         -- default campaign
    group_name VARCHAR(255),                               -- domain group
    status status_mood default 'pending',                  -- status ('pending', 'success', 'error')
    ssl_status ssl_status_mood default 'not_started',                  -- status ('not_started','pending', 'success', 'error')
    tenant_id INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT NOW(),                    -- creation date
    updated_at TIMESTAMP DEFAULT NOW(),                     -- update date
    CONSTRAINT domains_tenant_domain_key UNIQUE (tenant_id, domain)
);

-- Add a demo domain
INSERT INTO domains (domain, redirect_https, handle_404, default_campaign_id, group_name, status, created_at, updated_at, tenant_id)
VALUES
('demo.example.com', TRUE, 'error', NULL, 'Demo Group', 'pending', NOW(), NOW(), 1)
ON CONFLICT (tenant_id, domain) DO NOTHING;

CREATE TABLE IF NOT EXISTS landings (
    id SERIAL PRIMARY KEY,
    folder VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    link VARCHAR(255),
    type landing_mood,                  -- type ('link', 'mirror', 'file')
    tags VARCHAR(255),
    tenant_id INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT landings_tenant_folder_key UNIQUE (tenant_id, folder),
    CONSTRAINT landings_tenant_name_key UNIQUE (tenant_id, name)
);

INSERT INTO landings (folder, name, link, type, tags, created_at, updated_at, tenant_id)
VALUES
('demo_folder', 'Demo Landing', 'https://example.com/demo', 'link', 'demo,example', now(), now(), 1)
ON CONFLICT (tenant_id, folder) DO NOTHING;


CREATE TABLE IF NOT EXISTS affiliate_networks (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    offer_parameters VARCHAR(1024),
    s2s_postback VARCHAR(1024),
    tenant_id INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    CONSTRAINT affiliate_networks_tenant_name_key UNIQUE (tenant_id, name)
);


CREATE TABLE IF NOT EXISTS offers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    url TEXT NOT NULL,
    affiliate_network_id INTEGER REFERENCES affiliate_networks(id) ON DELETE SET NULL,
    countries JSONB,                                        -- [{ "code": "US", "priority": 1 }, { "code": "CA" }]
    payout NUMERIC(10, 2),
    currency VARCHAR(10) DEFAULT 'USD',
    status VARCHAR(20) DEFAULT 'active',
    tokens JSONB,
    notes TEXT,
    tags TEXT[],
    tenant_id INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    CONSTRAINT offers_tenant_name_key UNIQUE (tenant_id, name)
);

INSERT INTO offers (name, url, affiliate_network_id, countries, payout, currency, status, tokens, notes, tags, tenant_id)
VALUES
('Demo Offer 1', 'https://example.com/offer1',
 (SELECT id FROM affiliate_networks WHERE name = 'AdCombo' AND tenant_id = 1),
 '[{"code": "US", "priority": 1}, {"code": "CA"}]'::jsonb, 10.00, 'USD', 'active', '{"token1": "value1"}'::jsonb, 'This is a demo offer 1', ARRAY['tag1', 'tag2'], 1),
('Demo Offer 2', 'https://example.com/offer2',
 (SELECT id FROM affiliate_networks WHERE name = 'ClickDealer' AND tenant_id = 1),
 '[{"code": "UK", "priority": 1}, {"code": "AU"}]'::jsonb, 15.50, 'USD', 'active', '{"token2": "value2"}'::jsonb, 'This is a demo offer 2', ARRAY['tag3', 'tag4'], 1)
ON CONFLICT (tenant_id, name) DO NOTHING;


-- Add demo networks
INSERT INTO affiliate_networks (name, offer_parameters, s2s_postback, tenant_id)
VALUES
('AdCombo', 'aff_id={aff_id}&subid={sub_id}', 'https://adcombo.com/postback?cid={clickid}&status={status}', 1)
ON CONFLICT (tenant_id, name) DO NOTHING;

INSERT INTO affiliate_networks (name, offer_parameters, s2s_postback, tenant_id)
VALUES
('ClickDealer', 'aff_sub={subid}&click_id={cid}', 'https://clickdealer.com/pb?cid={cid}&conversion={conversion_status}', 1)
ON CONFLICT (tenant_id, name) DO NOTHING;

CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    traffic_loss FLOAT,
    s2s_postback VARCHAR(1024),
    s2s_postback_statuses JSONB,        -- {"sale": true, "lead": false, ...}
    settings JSONB,                     -- array of [{"name": ..., "parameter": ..., "token": ..., "editable_name": ...}]
    additional_settings JSONB,          -- arbitrary per-source extras: API keys etc. {"taboola_api_key": "..."}
    tenant_id INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    CONSTRAINT sources_tenant_name_key UNIQUE (tenant_id, name)
);

INSERT INTO sources (name, traffic_loss, s2s_postback, s2s_postback_statuses, settings, additional_settings, tenant_id)
VALUES
('Taboola US', 0.05, 'https://example.com/postback?clickid={clickid}',
 '{"sale": true, "lead": true, "reject": false, "upsell": false}',
 '[
  {"name": "Keyword", "parameter": "keyword", "token": "", "editable_name": false},
  {"name": "Cost", "parameter": "cost", "token": "", "editable_name": false},
  {"name": "Sub id 1", "parameter": "sub_id_1", "token": "", "editable_name": true},
  {"name": "Sub id 2", "parameter": "sub_id_2", "token": "", "editable_name": true}
 ]'::jsonb,
 '{}'::jsonb, 1) ON CONFLICT (tenant_id, name) DO NOTHING;

CREATE TABLE IF NOT EXISTS settings (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    value TEXT NOT NULL,
    tenant_id INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT settings_tenant_name_key UNIQUE (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    alias VARCHAR(255) NOT NULL,
    type campaign_type DEFAULT 'campaign',
    status campaign_status DEFAULT 'active',
    redirect_mode redirect_mode DEFAULT 'position',
    domain_id INTEGER REFERENCES domains(id) ON DELETE SET NULL,
    traffic_source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    config JSONB,
    notes TEXT,
    tags JSONB DEFAULT '[]'::jsonb,
    ad_platform_campaign_id VARCHAR(64),
    tenant_id INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    CONSTRAINT campaigns_tenant_name_key UNIQUE (tenant_id, name),
    CONSTRAINT campaigns_tenant_alias_key UNIQUE (tenant_id, alias)
);

-- Meta Ads cost auto-sync: the ad-platform campaign id is added idempotently
-- for installs created before the column existed (matches the startup _mig).
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS ad_platform_campaign_id VARCHAR(64);

-- Meta Ads cost auto-sync: raw daily platform audit trail. One row per
-- platform + ad account + platform campaign + day; the UNIQUE key backs the
-- idempotent upsert so re-syncing a day replaces its totals.
CREATE TABLE IF NOT EXISTS ad_cost_daily (
    id BIGSERIAL PRIMARY KEY,
    platform VARCHAR(32) NOT NULL DEFAULT 'meta',
    ad_account_id VARCHAR(64) NOT NULL,
    platform_campaign_id VARCHAR(64) NOT NULL DEFAULT '',
    campaign_name VARCHAR(255) NOT NULL DEFAULT '',
    date DATE NOT NULL,
    spend DOUBLE PRECISION NOT NULL DEFAULT 0,
    impressions BIGINT NOT NULL DEFAULT 0,
    clicks BIGINT NOT NULL DEFAULT 0,
    matched_campaign_id INTEGER,
    synced_at TIMESTAMP NOT NULL DEFAULT now(),
    tenant_id INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT ad_cost_daily_tenant_platform_key
        UNIQUE (tenant_id, platform, ad_account_id, platform_campaign_id, date)
);
CREATE INDEX IF NOT EXISTS ad_cost_daily_date_idx ON ad_cost_daily (date);

-- Ad-platform OAuth: one stored token per platform *per tenant* (the composite
-- UNIQUE backs the upsert in app_pages/integrations.py) and the single-use CSRF
-- state with a 10-minute TTL (deleted on consumption). access_token is masked
-- in API responses and nulled in the settings export.
CREATE TABLE IF NOT EXISTS integration_connections (
    id BIGSERIAL PRIMARY KEY,
    platform VARCHAR(32) NOT NULL,
    access_token TEXT,
    token_type VARCHAR(32) NOT NULL DEFAULT 'bearer',
    expires_at TIMESTAMP,
    scopes TEXT NOT NULL DEFAULT '',
    account_label VARCHAR(255) NOT NULL DEFAULT '',
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    tenant_id INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT integration_connections_tenant_platform_key UNIQUE (tenant_id, platform)
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    platform VARCHAR(32) NOT NULL,
    username VARCHAR(255) NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS oauth_states_created_at_idx ON oauth_states (created_at);

CREATE TABLE IF NOT EXISTS conversions_data (
    id SERIAL PRIMARY KEY,
    received_at TIMESTAMP DEFAULT NOW(),
    click_id VARCHAR(100),
    campaign_id INTEGER,
    offer_id INTEGER,
    landing_id INTEGER,
    ad_campaign_id VARCHAR(100),
    status conversion_status,
    external_id VARCHAR(100),
    payout REAL,
    revenue REAL,
    profit REAL,
    currency VARCHAR(10),
    transaction_id VARCHAR(100),
    country VARCHAR(50),
    region VARCHAR(50),
    city VARCHAR(50),
    ip INET,
    visitor_id VARCHAR(50),
    sub_id_1 VARCHAR(50),
    sub_id_2 VARCHAR(50),
    sub_id_3 VARCHAR(50),
    sub_id_4 VARCHAR(50),
    sub_id_5 VARCHAR(50),
    sub_id_6 VARCHAR(50),
    sub_id_7 VARCHAR(50),
    sub_id_8 VARCHAR(50),
    sub_id_9 VARCHAR(50),
    sub_id_10 VARCHAR(50),
    utm_campaign VARCHAR(50),
    utm_creative VARCHAR(50),
    utm_source VARCHAR(50),
    traffic_source_name VARCHAR(100),
    os VARCHAR(100),
    isp VARCHAR(100),
    is_using_proxy BOOLEAN,
    is_bot BOOLEAN,
    device_type VARCHAR(50),
    postback_count INTEGER DEFAULT 0,          -- how many postbacks this conversion received
    last_postback_at TIMESTAMP,                -- time of the most recent postback
    approval VARCHAR(16) NOT NULL DEFAULT 'pending', -- network reconciliation: pending|approved|declined|other
    is_duplicate BOOLEAN NOT NULL DEFAULT false,     -- row absorbed a deduplicated/dedupe-matched write
    fbc TEXT,                                  -- Meta click id (fb.1.<ms>.<fbclid>)
    fbp TEXT,                                  -- Meta browser cookie (_fbp)
    tenant_id INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_conversions_received_at ON conversions_data(received_at);
CREATE INDEX IF NOT EXISTS idx_conversions_click_id ON conversions_data(click_id);
CREATE INDEX IF NOT EXISTS idx_conversions_status ON conversions_data(status);


-- Add initial data
INSERT INTO settings (name, value, tenant_id) VALUES
('settings', '{
  "domain": "",
  "currency": "USD",
  "timezone": "UTC",
  "autoUpdateReports": true,
  "apiToken": "a1b2c3d4e5f6",
  "enableLogging": false
}', 1) ON CONFLICT (tenant_id, name) DO NOTHING;

INSERT INTO settings (name, value, tenant_id) VALUES
('subIdMapping', '[
  {"name":"Cost","parameter":"cost","token":"","editable_name":false},
  {"name":"Currency","parameter":"currency","token":"","editable_name":false},
  {"name":"External ID","parameter":"external_id","token":"","editable_name":false},
  {"name":"Creative ID","parameter":"utm_creative","token":"{{ad.name}}","editable_name":false},
  {"name":"AD Campaign ID","parameter":"utm_campaign","token":"{{campaign.name}}","editable_name":false},
  {"name":"Keyword","parameter":"keyword","token":"","editable_name":false},
  {"name":"Site","parameter":"utm_source","token":"{{site_source_name}}","editable_name":false},
  {"name":"Sub id 1","parameter":"sub_id_1","token":"","editable_name":true},
  {"name":"Sub id 2","parameter":"sub_id_2","token":"","editable_name":true},
  {"name":"Sub id 3","parameter":"sub_id_3","token":"","editable_name":true},
  {"name":"Sub id 4","parameter":"sub_id_4","token":"","editable_name":true},
  {"name":"Sub id 5","parameter":"sub_id_5","token":"","editable_name":true},
  {"name":"Sub id 6","parameter":"sub_id_6","token":"","editable_name":true},
  {"name":"Sub id 7","parameter":"sub_id_7","token":"","editable_name":true},
  {"name":"Sub id 8","parameter":"sub_id_8","token":"","editable_name":true},
  {"name":"Sub id 9","parameter":"sub_id_9","token":"","editable_name":true},
  {"name":"Sub id 10","parameter":"sub_id_10","token":"","editable_name":true}
]', 1) ON CONFLICT (tenant_id, name) DO NOTHING;


-- Create the initial tracker_admin user
INSERT INTO users (username, email, password_hash, is_admin, active)
VALUES (
    'tracker_admin',
    'admin@example.com',
    '5dfc9a6ef90c0908795b917ae279e90a', /* akm_ + admin */
    TRUE,
    TRUE
) ON CONFLICT (username) DO NOTHING;

INSERT INTO campaigns (name,alias, type, status, redirect_mode, domain_id, traffic_source_id, config, notes, created_at, updated_at, tenant_id)
SELECT
    'Campaign Demo 1', 'alias1', 'campaign', 'active', 'position',
    (SELECT id FROM domains ORDER BY id LIMIT 1),
    (SELECT id FROM sources ORDER BY id LIMIT 1),
    '{"integration_method": "php", "send_se_referrer": true, "use_title_as_keyword": true, "send_query_params": true, "bind_method": "full", "bind_ttl_hours": 24, "cost_model": "cpc", "traffic_loss_percent": 0, "cost": 0, "cost_currency": "USD", "cost_from_cost_parameter": false, "paramsIdMapping": [{"name": "Keyword", "parameter": "keyword", "token": ""}, {"name": "Cost", "parameter": "cost", "token": ""}, {"name": "Currency", "parameter": "currency", "token": ""}, {"name": "External ID", "parameter": "external_id", "token": ""}, {"name": "Creative ID", "parameter": "utm_creative", "token": "{{ad.name}}"}, {"name": "AD Campaign ID", "parameter": "utm_campaign", "token": "{{campaign.name}}"}, {"name": "Site", "parameter": "utm_source", "token": "{{site_source_name}}"}], "postbacks": [], "flows": []}',
    'Demo notes for campaign 1', '2025-05-01 00:00:00', '2025-05-01 00:00:00', 1
WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE alias = 'alias1' AND tenant_id = 1)
ON CONFLICT (tenant_id, name) DO NOTHING;

INSERT INTO campaigns (name, alias, type, status, redirect_mode, domain_id, traffic_source_id, config, notes, created_at, updated_at, tenant_id)
SELECT
    'Campaign Demo 2', 'alias2', 'campaign', 'active', 'position',
    (SELECT id FROM domains ORDER BY id LIMIT 1),
    (SELECT id FROM sources ORDER BY id LIMIT 1),
    '{"integration_method": "php", "send_se_referrer": true, "use_title_as_keyword": true, "send_query_params": true, "bind_method": "full", "bind_ttl_hours": 24, "cost_model": "cpc", "traffic_loss_percent": 0, "cost": 0, "cost_currency": "USD", "cost_from_cost_parameter": false, "paramsIdMapping": [{"name": "Keyword", "parameter": "keyword", "token": ""}, {"name": "Cost", "parameter": "cost", "token": ""}, {"name": "Currency", "parameter": "currency", "token": ""}, {"name": "External ID", "parameter": "external_id", "token": ""}, {"name": "Creative ID", "parameter": "utm_creative", "token": "{{ad.name}}"}, {"name": "AD Campaign ID", "parameter": "utm_campaign", "token": "{{campaign.name}}"}, {"name": "Site", "parameter": "utm_source", "token": "{{site_source_name}}"}], "postbacks": [], "flows": []}',
    'Demo notes for campaign 2', '2025-05-01 00:00:00', '2025-05-01 00:00:00', 1
WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE alias = 'alias2' AND tenant_id = 1)
ON CONFLICT (tenant_id, name) DO NOTHING;

INSERT INTO campaigns (name,alias,  type, status, redirect_mode, domain_id, traffic_source_id, config, notes, created_at, updated_at, tenant_id)
SELECT
    'Campaign Demo 3', 'alias3', 'campaign', 'active', 'position',
    (SELECT id FROM domains ORDER BY id LIMIT 1),
    (SELECT id FROM sources ORDER BY id LIMIT 1),
    '{"integration_method": "php", "send_se_referrer": true, "use_title_as_keyword": true, "send_query_params": true, "bind_method": "full", "bind_ttl_hours": 24, "cost_model": "cpc", "traffic_loss_percent": 0, "cost": 0, "cost_currency": "USD", "cost_from_cost_parameter": false, "paramsIdMapping": [{"name": "Keyword", "parameter": "keyword", "token": ""}, {"name": "Cost", "parameter": "cost", "token": ""}, {"name": "Currency", "parameter": "currency", "token": ""}, {"name": "External ID", "parameter": "external_id", "token": ""}, {"name": "Creative ID", "parameter": "utm_creative", "token": "{{ad.name}}"}, {"name": "AD Campaign ID", "parameter": "utm_campaign", "token": "{{campaign.name}}"}, {"name": "Site", "parameter": "utm_source", "token": "{{site_source_name}}"}], "postbacks": [], "flows": []}',
    'Demo notes for campaign 3', '2025-05-01 00:00:00', '2025-05-01 00:00:00', 1
WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE alias = 'alias3' AND tenant_id = 1)
ON CONFLICT (tenant_id, name) DO NOTHING;

-- Ensure the built-in admin is a member of tenant 1 even on an install whose
-- users were created after the backfill above ran (belt and braces; the app's
-- startup hook re-runs the same backfill on every boot).
INSERT INTO tenant_memberships (user_id, tenant_id, role, permissions)
SELECT u.id, 1,
       CASE WHEN u.is_admin AND u.id = (SELECT MIN(id) FROM users WHERE is_admin)
                 THEN 'owner'
            WHEN u.is_admin THEN 'admin'
            ELSE 'editor' END,
       u.permissions
FROM users u
-- Only users with NO membership at all (a user provisioned into another
-- workspace later must not be silently added to tenant 1).
WHERE NOT EXISTS (SELECT 1 FROM tenant_memberships m
                  WHERE m.user_id = u.id);
