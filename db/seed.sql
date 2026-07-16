INSERT INTO companies (org_id, name, industry, employee_count, annual_revenue_usd, country) VALUES
(1, 'Acme Robotics',     'Manufacturing', 1200, 250000000, 'US'),  -- id 1: big + high revenue
(1, 'BlueFin Analytics', 'SaaS',           120,   8000000, 'UK'),  -- id 2: mid size, low revenue
(1, 'Karma Textiles',    'Retail',          30,   2000000, 'IN'),  -- id 3: tiny
(1, 'Nimbus Health',     'Healthcare',     600,  80000000, 'US'),  -- id 4: big, existing customer
(1, 'Vertex FinTech',    'FinTech',        250,  40000000, 'US');  -- id 5: mid, lost-deal history

INSERT INTO leads (org_id, email, first_name, last_name, title, company_id, source) VALUES
-- DEMO PATH 1 — AUTO-EXECUTE: score 100 (VP + big co + high rev + webinar) AND no
-- blocker (Acme has an open DEAL + a resolved ticket, neither of which gates) →
-- score >= 80 with nothing blocking → gate auto-executes. The only clean
-- auto-route path in the seed.
(1, 'priya@acmerobotics.com',  'Priya',  'Sharma',  'VP of Operations',  1, 'webinar'),
-- DEMO PATH 4 — DISCARD: score 0 (junior title + tiny co + cold list) → the model
-- proposes discard; below threshold so the gate still routes it to a human.
(1, 'sam@karmatextiles.in',    'Sam',    'Okoro',   'Data Analyst',      3, 'cold_list'),
-- DEMO PATH 3 — THRESHOLD-GATE: score 65 (Founder + mid co + low rev + form) → warm,
-- under the 80 auto-execute gate, no blocker → pending_approval on score alone.
(1, 'lena@bluefin.io',         'Lena',   'Fischer', 'Founder',           2, 'website_form'),
-- DEMO PATH 2 — BLOCKER-GATE: score 90 (Director + big co + rev + webinar), hot AND an
-- existing customer (won deal), but Nimbus has an OPEN TICKET → blocker fires and
-- overrides the high score → pending_approval. High score does not bypass a blocker.
(1, 'mark@nimbushealth.com',   'Mark',   'Reilly',  'Director of IT',    4, 'webinar'),
(1, 'tom@bluefin.io',          'Tom',    'Wells',   'Data Analyst',      2, 'website_form'), -- ~35
(1, 'rachel@vertexfintech.com','Rachel', 'Kim',     'Head of Growth',    5, 'referral'),     -- ~60
(1, 'dan@acmerobotics.com',    'Dan',    'Alvarez', 'Sales Coordinator', 1, 'website_form'), -- ~70: junior person, big company
(1, 'noreply@leadgenblast.co', 'Growth', 'Ninja',   'Growth Ninja',   NULL, 'cold_list');    -- spam: no company → 0

INSERT INTO deals (org_id, company_id, name, stage, amount_usd, closed_at) VALUES
(1, 1, 'Acme pilot expansion',  'open', 45000, NULL),          -- Priya's company has an open deal
(1, 4, 'Nimbus annual license', 'won',  60000, '2026-02-10'),  -- Nimbus = existing customer
(1, 5, 'Vertex platform deal',  'lost', 30000, '2025-11-20'),  -- previously lost → "needs review"
(1, 2, 'BlueFin starter',       'open',  5000, NULL);

INSERT INTO tickets (org_id, company_id, subject, status) VALUES
(1, 4, 'API rate limit questions', 'open'),       -- good customer, but has an open ticket (edge case)
(1, 2, 'SSO login failing',        'resolved'),
(1, 1, 'Onboarding docs request',  'resolved');

-- org 2: same company name AND same lead email as org 1, under org_id = 2.
-- Proves the composite uniques are (org_id, name) / (org_id, email) — not name/email
-- alone — by inserting duplicates that only differ by org_id.
INSERT INTO companies (org_id, name, industry, employee_count, annual_revenue_usd, country) VALUES
(2, 'Acme Robotics', 'Manufacturing', 1200, 250000000, 'US');  -- id 6: same name as id 1, org 2

-- Deliberately a DIFFERENT person behind the same address, with a different
-- title and source, so a cross-tenant read is visible in the payload rather
-- than looking like a duplicate of org 1's Priya.
INSERT INTO leads (org_id, email, first_name, last_name, title, company_id, source) VALUES
(2, 'priya@acmerobotics.com', 'Priyanka', 'Rao', 'Head of Data',
    (SELECT id FROM companies WHERE org_id = 2 AND name = 'Acme Robotics'), 'referral');

-- org 2's Acme has its OWN CRM history, disjoint from org 1's Acme. Without
-- these rows a leak and a correct scope look the same (both "no deals"), so the
-- cross-tenant tests would pass whether or not the filter worked.
INSERT INTO deals (org_id, company_id, name, stage, amount_usd, closed_at) VALUES
(2, (SELECT id FROM companies WHERE org_id = 2 AND name = 'Acme Robotics'),
    'ORG2-CONFIDENTIAL Acme renewal', 'lost', 990000, '2026-01-05');

INSERT INTO tickets (org_id, company_id, subject, status) VALUES
(2, (SELECT id FROM companies WHERE org_id = 2 AND name = 'Acme Robotics'),
    'ORG2-CONFIDENTIAL data migration escalation', 'open');
