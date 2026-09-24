-- Test traffic for verification: 500 clicks over ~20 days, campaign 1 / offer 1.
-- Run: docker exec -i tracker_clickhouse clickhouse-client --user user --password password_password_password < seed-test-data.sql
-- Delete later with: ALTER TABLE clicks_data DELETE WHERE campaign_id = 1 AND visitor_id LIKE 'seed-%'
INSERT INTO clicks_data (received_at, campaign_id, offer_id, click, status, visitor_id, country, device_type, os, browser, language, url, referrer, keyword, utm_source, utm_campaign, traffic_source_name, ip, cost, revenue, profit)
SELECT
    now() - INTERVAL number HOUR,
    1,
    1,
    if(number % 3 = 0, true, false) AS click,
    multiIf(number % 40 = 0, 'sale', number % 25 = 0, 'lead', number % 60 = 0, 'rejected', ''),
    'seed-' || toString(100000 + number),
    ['US','IN','GB','BR','DE'][1 + number % 5],
    ['Mobile','Desktop','Tablet'][1 + number % 3],
    ['Android','iOS','Windows'][1 + number % 3],
    ['Chrome','Safari','Firefox'][1 + number % 3],
    'en',
    'https://example.com/landing',
    'https://fb.com/feed',
    'kw' || toString(number % 7),
    'facebook',
    'test-campaign',
    'Facebook',
    toIPv4(concat('10.0.', toString(number % 250), '.', toString((number * 7) % 250))),
    if(number % 3 = 0, 0.05, 0),
    multiIf(number % 40 = 0, 5.0, number % 25 = 0, 1.0, 0),
    multiIf(number % 40 = 0, 5.0, number % 25 = 0, 1.0, 0)
FROM numbers(500);