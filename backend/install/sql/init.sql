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





CREATE TABLE IF NOT EXISTS domains (
    id SERIAL PRIMARY KEY,
    domain VARCHAR(255) UNIQUE NOT NULL,                  -- domain address
    redirect_https BOOLEAN DEFAULT TRUE,                  -- whether to redirect to https
    handle_404 domain_error_handle_mood,                -- 'error' or 'redirect_to_company'
    default_campaign_id INTEGER,                         -- default campaign
    group_name VARCHAR(255),                               -- domain group
    status status_mood default 'pending',                  -- status ('pending', 'success', 'error')
    ssl_status ssl_status_mood default 'not_started',                  -- status ('not_started','pending', 'success', 'error')
    created_at TIMESTAMP DEFAULT NOW(),                    -- creation date
    updated_at TIMESTAMP DEFAULT NOW()                     -- update date
);

-- Add a demo domain
INSERT INTO domains (domain, redirect_https, handle_404, default_campaign_id, group_name, status, created_at, updated_at)
VALUES
('demo.example.com', TRUE, 'error', NULL, 'Demo Group', 'pending', NOW(), NOW()) ON CONFLICT (domain) DO NOTHING;

CREATE TABLE IF NOT EXISTS landings (
    id SERIAL PRIMARY KEY,
    folder VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL UNIQUE,
    link VARCHAR(255),
    type landing_mood,                  -- type ('link', 'mirror', 'file')
    tags VARCHAR(255),
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO landings (folder, name, link, type, tags, created_at, updated_at)
VALUES
('demo_folder', 'Demo Landing', 'https://example.com/demo', 'link', 'demo,example', now(), now()) ON CONFLICT (folder) DO NOTHING;


CREATE TABLE IF NOT EXISTS affiliate_networks (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    offer_parameters VARCHAR(1024),
    s2s_postback VARCHAR(1024),
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);


CREATE TABLE IF NOT EXISTS offers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    url TEXT NOT NULL,
    affiliate_network_id INTEGER REFERENCES affiliate_networks(id) ON DELETE SET NULL,
    countries JSONB,                                        -- [{ "code": "US", "priority": 1 }, { "code": "CA" }]
    payout NUMERIC(10, 2),
    currency VARCHAR(10) DEFAULT 'USD',
    status VARCHAR(20) DEFAULT 'active',
    tokens JSONB,
    notes TEXT,
    tags TEXT[],
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

INSERT INTO offers (name, url, affiliate_network_id, countries, payout, currency, status, tokens, notes, tags)
VALUES
('Demo Offer 1', 'https://example.com/offer1',
 (SELECT id FROM affiliate_networks WHERE name = 'AdCombo'),
 '[{"code": "US", "priority": 1}, {"code": "CA"}]'::jsonb, 10.00, 'USD', 'active', '{"token1": "value1"}'::jsonb, 'This is a demo offer 1', ARRAY['tag1', 'tag2']),
('Demo Offer 2', 'https://example.com/offer2',
 (SELECT id FROM affiliate_networks WHERE name = 'ClickDealer'),
 '[{"code": "UK", "priority": 1}, {"code": "AU"}]'::jsonb, 15.50, 'USD', 'active', '{"token2": "value2"}'::jsonb, 'This is a demo offer 2', ARRAY['tag3', 'tag4']) ON CONFLICT (name) DO NOTHING;


-- Add demo networks
INSERT INTO affiliate_networks (name, offer_parameters, s2s_postback)
VALUES
('AdCombo', 'aff_id={aff_id}&subid={sub_id}', 'https://adcombo.com/postback?cid={clickid}&status={status}')
ON CONFLICT (name) DO NOTHING;

INSERT INTO affiliate_networks (name, offer_parameters, s2s_postback)
VALUES
('ClickDealer', 'aff_sub={subid}&click_id={cid}', 'https://clickdealer.com/pb?cid={cid}&conversion={conversion_status}')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    traffic_loss FLOAT,
    s2s_postback VARCHAR(1024),
    s2s_postback_statuses JSONB,        -- {"sale": true, "lead": false, ...}
    settings JSONB,                     -- array of [{"name": ..., "parameter": ..., "token": ..., "editable_name": ...}]
    additional_settings JSONB,          -- arbitrary per-source extras: API keys etc. {"taboola_api_key": "..."}
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

INSERT INTO sources (name, traffic_loss, s2s_postback, s2s_postback_statuses, settings, additional_settings)
VALUES
('Taboola US', 0.05, 'https://example.com/postback?clickid={clickid}',
 '{"sale": true, "lead": true, "reject": false, "upsell": false}',
 '[
  {"name": "Keyword", "parameter": "keyword", "token": "", "editable_name": false},
  {"name": "Cost", "parameter": "cost", "token": "", "editable_name": false},
  {"name": "Sub id 1", "parameter": "sub_id_1", "token": "", "editable_name": true},
  {"name": "Sub id 2", "parameter": "sub_id_2", "token": "", "editable_name": true}
 ]'::jsonb,
 '{}'::jsonb) ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS settings (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    alias VARCHAR(255) NOT NULL UNIQUE,
    type campaign_type DEFAULT 'campaign',
    status campaign_status DEFAULT 'active',
    redirect_mode redirect_mode DEFAULT 'position',
    domain_id INTEGER REFERENCES domains(id) ON DELETE SET NULL,
    traffic_source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    config JSONB,
    notes TEXT,
    tags JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

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
    fbc TEXT,                                  -- Meta click id (fb.1.<ms>.<fbclid>)
    fbp TEXT                                   -- Meta browser cookie (_fbp)
);

CREATE INDEX IF NOT EXISTS idx_conversions_received_at ON conversions_data(received_at);
CREATE INDEX IF NOT EXISTS idx_conversions_click_id ON conversions_data(click_id);
CREATE INDEX IF NOT EXISTS idx_conversions_status ON conversions_data(status);


-- Add initial data
INSERT INTO settings (name, value) VALUES
('settings', '{
  "domain": "",
  "currency": "USD",
  "timezone": "UTC",
  "autoUpdateReports": true,
  "apiToken": "a1b2c3d4e5f6",
  "enableLogging": false
}') ON CONFLICT (name) DO NOTHING;

INSERT INTO settings (name, value) VALUES
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
]') ON CONFLICT (name) DO NOTHING;


-- Create the initial tracker_admin user
INSERT INTO users (username, email, password_hash, is_admin, active)
VALUES (
    'tracker_admin',
    'admin@example.com',
    '5dfc9a6ef90c0908795b917ae279e90a', /* akm_ + admin */
    TRUE,
    TRUE
) ON CONFLICT (username) DO NOTHING;

INSERT INTO campaigns (name,alias, type, status, redirect_mode, domain_id, traffic_source_id, config, notes, created_at, updated_at)
SELECT
    'Campaign Demo 1', 'alias1', 'campaign', 'active', 'position',
    (SELECT id FROM domains ORDER BY id LIMIT 1),
    (SELECT id FROM sources ORDER BY id LIMIT 1),
    '{"integration_method": "php", "send_se_referrer": true, "use_title_as_keyword": true, "send_query_params": true, "bind_method": "full", "bind_ttl_hours": 24, "cost_model": "cpc", "traffic_loss_percent": 0, "cost": 0, "cost_currency": "USD", "cost_from_cost_parameter": false, "paramsIdMapping": [{"name": "Keyword", "parameter": "keyword", "token": ""}, {"name": "Cost", "parameter": "cost", "token": ""}, {"name": "Currency", "parameter": "currency", "token": ""}, {"name": "External ID", "parameter": "external_id", "token": ""}, {"name": "Creative ID", "parameter": "utm_creative", "token": "{{ad.name}}"}, {"name": "AD Campaign ID", "parameter": "utm_campaign", "token": "{{campaign.name}}"}, {"name": "Site", "parameter": "utm_source", "token": "{{site_source_name}}"}], "postbacks": [], "flows": []}',
    'Demo notes for campaign 1', '2025-05-01 00:00:00', '2025-05-01 00:00:00'
WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE alias = 'alias1')
ON CONFLICT (name) DO NOTHING;

INSERT INTO campaigns (name, alias, type, status, redirect_mode, domain_id, traffic_source_id, config, notes, created_at, updated_at)
SELECT
    'Campaign Demo 2', 'alias2', 'campaign', 'active', 'position',
    (SELECT id FROM domains ORDER BY id LIMIT 1),
    (SELECT id FROM sources ORDER BY id LIMIT 1),
    '{"integration_method": "php", "send_se_referrer": true, "use_title_as_keyword": true, "send_query_params": true, "bind_method": "full", "bind_ttl_hours": 24, "cost_model": "cpc", "traffic_loss_percent": 0, "cost": 0, "cost_currency": "USD", "cost_from_cost_parameter": false, "paramsIdMapping": [{"name": "Keyword", "parameter": "keyword", "token": ""}, {"name": "Cost", "parameter": "cost", "token": ""}, {"name": "Currency", "parameter": "currency", "token": ""}, {"name": "External ID", "parameter": "external_id", "token": ""}, {"name": "Creative ID", "parameter": "utm_creative", "token": "{{ad.name}}"}, {"name": "AD Campaign ID", "parameter": "utm_campaign", "token": "{{campaign.name}}"}, {"name": "Site", "parameter": "utm_source", "token": "{{site_source_name}}"}], "postbacks": [], "flows": []}',
    'Demo notes for campaign 2', '2025-05-01 00:00:00', '2025-05-01 00:00:00'
WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE alias = 'alias2')
ON CONFLICT (name) DO NOTHING;

INSERT INTO campaigns (name,alias,  type, status, redirect_mode, domain_id, traffic_source_id, config, notes, created_at, updated_at)
SELECT
    'Campaign Demo 3', 'alias3', 'campaign', 'active', 'position',
    (SELECT id FROM domains ORDER BY id LIMIT 1),
    (SELECT id FROM sources ORDER BY id LIMIT 1),
    '{"integration_method": "php", "send_se_referrer": true, "use_title_as_keyword": true, "send_query_params": true, "bind_method": "full", "bind_ttl_hours": 24, "cost_model": "cpc", "traffic_loss_percent": 0, "cost": 0, "cost_currency": "USD", "cost_from_cost_parameter": false, "paramsIdMapping": [{"name": "Keyword", "parameter": "keyword", "token": ""}, {"name": "Cost", "parameter": "cost", "token": ""}, {"name": "Currency", "parameter": "currency", "token": ""}, {"name": "External ID", "parameter": "external_id", "token": ""}, {"name": "Creative ID", "parameter": "utm_creative", "token": "{{ad.name}}"}, {"name": "AD Campaign ID", "parameter": "utm_campaign", "token": "{{campaign.name}}"}, {"name": "Site", "parameter": "utm_source", "token": "{{site_source_name}}"}], "postbacks": [], "flows": []}',
    'Demo notes for campaign 3', '2025-05-01 00:00:00', '2025-05-01 00:00:00'
WHERE NOT EXISTS (SELECT 1 FROM campaigns WHERE alias = 'alias3')
ON CONFLICT (name) DO NOTHING;