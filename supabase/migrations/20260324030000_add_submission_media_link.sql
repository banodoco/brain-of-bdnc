ALTER TABLE public.ag_submissions
    ADD COLUMN IF NOT EXISTS media_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.ag_submissions'::regclass
          AND conname = 'ag_submissions_media_id_fkey'
    ) THEN
        ALTER TABLE public.ag_submissions
            ADD CONSTRAINT ag_submissions_media_id_fkey
            FOREIGN KEY (media_id)
            REFERENCES public.media(id)
            ON DELETE SET NULL;
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_ag_submissions_media_id
    ON public.ag_submissions(media_id)
    WHERE media_id IS NOT NULL;

UPDATE public.ag_submissions s
SET media_id = m.id
FROM public.media m
WHERE s.media_id IS NULL
  AND s.video_url IS NOT NULL
  AND m.url = s.video_url;

ALTER TABLE public.ag_submissions
    ALTER COLUMN video_url DROP NOT NULL;
