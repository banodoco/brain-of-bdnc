-- Fix RLS on discord_reactions: replace permissive FOR ALL policy
-- with read-only public access (matching all other tables).
-- Service role bypasses RLS entirely, so it still has full access.

DROP POLICY IF EXISTS "Service role full access" ON discord_reactions;

CREATE POLICY "Allow public read access to reactions" ON discord_reactions
    FOR SELECT USING (true);

-- No INSERT/UPDATE/DELETE policies = anon/authenticated cannot modify
