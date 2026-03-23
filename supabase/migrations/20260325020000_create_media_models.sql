-- Allow media items to be associated with multiple models (e.g. "this video was made with LTXV 13B")
-- Same pattern as asset_models.

CREATE TABLE IF NOT EXISTS public.media_models (
    media_id UUID NOT NULL REFERENCES public.media(id) ON DELETE CASCADE,
    model_id UUID NOT NULL REFERENCES public.models(id) ON DELETE CASCADE,
    compatibility_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (media_id, model_id)
);

CREATE INDEX idx_media_models_model_id ON public.media_models(model_id);

ALTER TABLE public.media_models ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read media_models" ON public.media_models;
CREATE POLICY "Public read media_models" ON public.media_models
    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Service write media_models" ON public.media_models;
CREATE POLICY "Service write media_models" ON public.media_models
    FOR ALL USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

GRANT SELECT ON public.media_models TO anon, authenticated;
