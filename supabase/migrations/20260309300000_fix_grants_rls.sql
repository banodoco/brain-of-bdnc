-- Fix grant_applications RLS: drop the overly permissive policy,
-- replace with read-only public access. Service role bypasses RLS
-- so the bot still has full write access.

DROP POLICY IF EXISTS "Allow service role full access on grant_applications" ON grant_applications;

CREATE POLICY "Allow public read access to grant_applications" ON grant_applications
    FOR SELECT USING (true);

-- No INSERT/UPDATE/DELETE policies = anon/authenticated cannot modify
