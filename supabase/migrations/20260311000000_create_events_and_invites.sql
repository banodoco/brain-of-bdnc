-- Events table
CREATE TABLE events (
  id SERIAL PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,          -- e.g. 'ados-paris-2026'
  name TEXT NOT NULL,                 -- e.g. 'ADOS Paris 2026'
  description TEXT,
  location TEXT,
  start_date DATE,
  end_date DATE,
  website_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Invite codes table
CREATE TABLE invite_codes (
  id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,  -- the code used in ?invite=
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  member_id BIGINT NOT NULL REFERENCES discord_members(member_id),
  display_name TEXT NOT NULL,         -- the name shown in the invitation
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  viewed_at TIMESTAMPTZ,             -- first time the invite was opened
  UNIQUE(event_id, member_id)        -- one invite per member per event
);

-- RLS
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE invite_codes ENABLE ROW LEVEL SECURITY;

-- Public read access (needed by the website)
CREATE POLICY "Public can read events" ON events FOR SELECT USING (true);
CREATE POLICY "Public can read invite_codes" ON invite_codes FOR SELECT USING (true);
