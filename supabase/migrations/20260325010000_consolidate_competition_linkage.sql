-- Consolidate competition linkage: use competition_content join table exclusively.
-- Drop direct competition_id FK columns from media and assets.

-- Migrate any existing data into competition_content
INSERT INTO public.competition_content (competition_id, content_type, content_id)
SELECT competition_id, 'media', id
FROM public.media
WHERE competition_id IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO public.competition_content (competition_id, content_type, content_id)
SELECT competition_id, 'asset', id
FROM public.assets
WHERE competition_id IS NOT NULL
ON CONFLICT DO NOTHING;

-- Drop from media
ALTER TABLE public.media DROP CONSTRAINT IF EXISTS media_competition_id_fkey;
DROP INDEX IF EXISTS public.idx_media_competition_id;
ALTER TABLE public.media DROP COLUMN IF EXISTS competition_id;

-- Drop from assets
ALTER TABLE public.assets DROP CONSTRAINT IF EXISTS assets_competition_id_fkey;
DROP INDEX IF EXISTS public.idx_assets_competition_id;
ALTER TABLE public.assets DROP COLUMN IF EXISTS competition_id;
