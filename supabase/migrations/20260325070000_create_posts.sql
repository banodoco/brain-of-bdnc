-- Posts: articles/content that can contain text and connect to media, models, assets.
-- Can be entered into competitions via competition_entries.post_id.

CREATE TABLE IF NOT EXISTS public.posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    member_id BIGINT NOT NULL REFERENCES public.members(member_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT,
    slug TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_posts_member_id ON public.posts(member_id);
CREATE INDEX idx_posts_status ON public.posts(status) WHERE status = 'published';
CREATE INDEX idx_posts_created_at ON public.posts(created_at DESC);

ALTER TABLE public.posts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read published posts" ON public.posts FOR SELECT
    USING (status = 'published' OR member_id IN (
        SELECT m.member_id FROM public.members m WHERE m.auth_user_id = auth.uid()
    ) OR public.is_admin());

CREATE POLICY "Users create own posts" ON public.posts FOR INSERT TO authenticated
    WITH CHECK (member_id IN (
        SELECT m.member_id FROM public.members m WHERE m.auth_user_id = auth.uid()
    ));

CREATE POLICY "Users update own posts" ON public.posts FOR UPDATE TO authenticated
    USING (member_id IN (
        SELECT m.member_id FROM public.members m WHERE m.auth_user_id = auth.uid()
    ) OR public.is_admin());

CREATE POLICY "Users delete own posts" ON public.posts FOR DELETE TO authenticated
    USING (member_id IN (
        SELECT m.member_id FROM public.members m WHERE m.auth_user_id = auth.uid()
    ) OR public.is_admin());

GRANT SELECT ON public.posts TO anon, authenticated;
GRANT INSERT, UPDATE, DELETE ON public.posts TO authenticated;

CREATE TRIGGER trg_posts_updated_at
    BEFORE UPDATE ON public.posts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Join tables

CREATE TABLE IF NOT EXISTS public.post_media (
    post_id UUID NOT NULL REFERENCES public.posts(id) ON DELETE CASCADE,
    media_id UUID NOT NULL REFERENCES public.media(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    caption TEXT,
    PRIMARY KEY (post_id, media_id)
);

CREATE INDEX idx_post_media_media_id ON public.post_media(media_id);

ALTER TABLE public.post_media ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read post_media" ON public.post_media FOR SELECT USING (true);
CREATE POLICY "Users manage own post_media" ON public.post_media FOR ALL TO authenticated
    USING (EXISTS (
        SELECT 1 FROM public.posts p
        JOIN public.members m ON m.member_id = p.member_id
        WHERE p.id = post_media.post_id AND m.auth_user_id = auth.uid()
    ));
GRANT SELECT ON public.post_media TO anon, authenticated;
GRANT INSERT, UPDATE, DELETE ON public.post_media TO authenticated;

CREATE TABLE IF NOT EXISTS public.post_models (
    post_id UUID NOT NULL REFERENCES public.posts(id) ON DELETE CASCADE,
    model_id UUID NOT NULL REFERENCES public.models(id) ON DELETE CASCADE,
    PRIMARY KEY (post_id, model_id)
);

CREATE INDEX idx_post_models_model_id ON public.post_models(model_id);

ALTER TABLE public.post_models ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read post_models" ON public.post_models FOR SELECT USING (true);
CREATE POLICY "Users manage own post_models" ON public.post_models FOR ALL TO authenticated
    USING (EXISTS (
        SELECT 1 FROM public.posts p
        JOIN public.members m ON m.member_id = p.member_id
        WHERE p.id = post_models.post_id AND m.auth_user_id = auth.uid()
    ));
GRANT SELECT ON public.post_models TO anon, authenticated;
GRANT INSERT, UPDATE, DELETE ON public.post_models TO authenticated;

CREATE TABLE IF NOT EXISTS public.post_assets (
    post_id UUID NOT NULL REFERENCES public.posts(id) ON DELETE CASCADE,
    asset_id UUID NOT NULL REFERENCES public.assets(id) ON DELETE CASCADE,
    PRIMARY KEY (post_id, asset_id)
);

CREATE INDEX idx_post_assets_asset_id ON public.post_assets(asset_id);

ALTER TABLE public.post_assets ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read post_assets" ON public.post_assets FOR SELECT USING (true);
CREATE POLICY "Users manage own post_assets" ON public.post_assets FOR ALL TO authenticated
    USING (EXISTS (
        SELECT 1 FROM public.posts p
        JOIN public.members m ON m.member_id = p.member_id
        WHERE p.id = post_assets.post_id AND m.auth_user_id = auth.uid()
    ));
GRANT SELECT ON public.post_assets TO anon, authenticated;
GRANT INSERT, UPDATE, DELETE ON public.post_assets TO authenticated;

-- Add post_id to competition_entries
ALTER TABLE public.competition_entries
    ADD COLUMN IF NOT EXISTS post_id UUID REFERENCES public.posts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_competition_entries_post_id
    ON public.competition_entries(post_id) WHERE post_id IS NOT NULL;
