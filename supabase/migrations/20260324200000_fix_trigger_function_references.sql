-- Fix triggers that still reference old ag_ function names after the rename.
-- The ALTER FUNCTION ... RENAME only changes the function name, not the trigger
-- definitions that call it. We need to drop and recreate affected triggers.

-- Vote IP capture trigger (calls capture_vote_ip which internally calls hash_ip_address)
DROP TRIGGER IF EXISTS trg_votes_capture_ip ON public.votes;
CREATE TRIGGER trg_votes_capture_ip
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.capture_vote_ip();

-- Analytics IP capture trigger
DROP TRIGGER IF EXISTS trg_submission_analytics_capture_ip ON public.submission_analytics;
CREATE TRIGGER trg_submission_analytics_capture_ip
    BEFORE INSERT ON public.submission_analytics
    FOR EACH ROW
    EXECUTE FUNCTION public.capture_analytics_ip();

-- Self-voting prevention trigger
DROP TRIGGER IF EXISTS trg_votes_prevent_self_voting ON public.votes;
CREATE TRIGGER trg_votes_prevent_self_voting
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_self_voting();

-- Max votes enforcement trigger
DROP TRIGGER IF EXISTS trg_votes_enforce_max_votes ON public.votes;
CREATE TRIGGER trg_votes_enforce_max_votes
    BEFORE INSERT ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_max_votes();

-- Vote count update trigger
DROP TRIGGER IF EXISTS trg_votes_update_submission_vote_count ON public.votes;
CREATE TRIGGER trg_votes_update_submission_vote_count
    AFTER INSERT OR DELETE ON public.votes
    FOR EACH ROW
    EXECUTE FUNCTION public.update_submission_vote_count();

-- Banodoco owner flag trigger on members
DROP TRIGGER IF EXISTS trg_members_banodoco_owner_flag ON public.members;
CREATE TRIGGER trg_members_banodoco_owner_flag
    BEFORE INSERT OR UPDATE OF member_id ON public.members
    FOR EACH ROW
    EXECUTE FUNCTION public.apply_banodoco_owner_flag();
