CREATE TABLE IF NOT EXISTS public.asset_models (
    asset_id UUID NOT NULL REFERENCES public.assets(id) ON DELETE CASCADE,
    model_id UUID NOT NULL REFERENCES public.models(id) ON DELETE CASCADE,
    compatibility_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asset_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_models_model_id
    ON public.asset_models(model_id);

ALTER TABLE public.asset_models ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read asset_models" ON public.asset_models;
CREATE POLICY "Public read asset_models"
    ON public.asset_models FOR SELECT
    USING (true);

DROP POLICY IF EXISTS "Service write asset_models" ON public.asset_models;
CREATE POLICY "Service write asset_models"
    ON public.asset_models FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

GRANT SELECT ON public.asset_models TO anon, authenticated;

INSERT INTO public.asset_models (asset_id, model_id, compatibility_note)
SELECT a.id, m.id, 'backfilled from assets.lora_base_model'
FROM public.assets a
JOIN public.models m ON m.display_name = a.lora_base_model
WHERE a.lora_base_model IS NOT NULL
ON CONFLICT (asset_id, model_id) DO NOTHING;
