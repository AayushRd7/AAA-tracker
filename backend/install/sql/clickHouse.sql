CREATE TABLE IF NOT EXISTS clicks_data (
    received_at DateTime DEFAULT now(),
    campaign_id Nullable(Int),
    offer_id Nullable(Int),
    ad_campaign_id String,
    click Nullable(Bool),
    status LowCardinality(String),
    external_id String,
    keyword String,
    landing_id String,
    language LowCardinality(String),
    url String,
    referrer String,
    browser LowCardinality(String),
    connection_type LowCardinality(String),
    currency LowCardinality(String),
    cost Nullable(Float32),
    profit Nullable(Float32),
    revenue Nullable(Float32),
    country LowCardinality(String),
    region LowCardinality(String),
    city LowCardinality(String),
    utm_campaign String,
    utm_creative String,
    utm_source String,
    visitor_id String,
    sub_id_1 String,
    sub_id_2 String,
    sub_id_3 String,
    sub_id_4 String,
    sub_id_5 String,
    sub_id_6 String,
    sub_id_7 String,
    sub_id_8 String,
    sub_id_9 String,
    sub_id_10 String,
    traffic_source_name LowCardinality(String),
    os LowCardinality(String),
    isp LowCardinality(String),
    ip IPv4,
    is_using_proxy Nullable(Bool),
    is_bot Nullable(Bool),
    device_type LowCardinality(String),
    flow_index UInt8 DEFAULT 0,
    utm_medium String DEFAULT '',
    impression UInt8 DEFAULT 0,
    fraud_score UInt8 DEFAULT 0,
    click_id String DEFAULT '',
    ip_full String DEFAULT '',
    fbc String DEFAULT '',
    fbp String DEFAULT '',
    -- Tenant that owns the click (written from the resolved campaign's
    -- tenant_id; DEFAULT 1 keeps rows written before multi-tenancy in
    -- tenant 1). Not part of ORDER BY: converting an existing MergeTree's
    -- sort key is impossible via ALTER and would make fresh installs
    -- structurally different from upgraded ones.
    tenant_id UInt32 DEFAULT 1
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(received_at)
ORDER BY (received_at)
SETTINGS index_granularity = 8192;
