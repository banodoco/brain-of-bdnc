-- Drop the submissions compatibility view now that the frontend
-- queries competition_entries directly.

DROP TRIGGER IF EXISTS trg_submissions_view_insert ON public.submissions;
DROP TRIGGER IF EXISTS trg_submissions_view_update ON public.submissions;
DROP TRIGGER IF EXISTS trg_submissions_view_delete ON public.submissions;

DROP VIEW IF EXISTS public.submissions;

DROP FUNCTION IF EXISTS public.submissions_view_insert();
DROP FUNCTION IF EXISTS public.submissions_view_update();
DROP FUNCTION IF EXISTS public.submissions_view_delete();
