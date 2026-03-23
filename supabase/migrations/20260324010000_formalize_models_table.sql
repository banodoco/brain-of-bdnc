-- Formalize the shared AI models catalog without rewriting existing rows.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.models (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name TEXT NOT NULL,
    default_variant TEXT,
    description TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.models
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS default_variant TEXT,
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_models_display_name
    ON public.models(display_name);

ALTER TABLE public.models ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read models" ON public.models;
CREATE POLICY "Public read models"
    ON public.models FOR SELECT
    USING (true);

DROP POLICY IF EXISTS "Service write models" ON public.models;
CREATE POLICY "Service write models"
    ON public.models FOR ALL
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');

DROP TRIGGER IF EXISTS trg_models_updated_at ON public.models;
CREATE TRIGGER trg_models_updated_at
    BEFORE UPDATE ON public.models
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();

GRANT SELECT ON public.models TO anon, authenticated;
