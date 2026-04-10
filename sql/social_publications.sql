create extension if not exists pgcrypto;

create or replace function public.set_social_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := timezone('utc', now());
    return new;
end;
$$;

create table if not exists public.social_publications (
    publication_id uuid primary key default gen_random_uuid(),
    guild_id bigint not null,
    channel_id bigint not null,
    message_id bigint not null,
    user_id bigint not null,
    source_kind text not null,
    platform text not null,
    action text not null check (action in ('post', 'reply', 'retweet')),
    route_key text,
    request_payload jsonb not null default '{}'::jsonb,
    target_post_ref text,
    integrity_version text,
    integrity_signature text,
    scheduled_at timestamptz not null default timezone('utc', now()),
    status text not null default 'queued' check (status in ('queued', 'processing', 'succeeded', 'failed', 'cancelled')),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    retry_after timestamptz,
    last_error text,
    provider_ref text,
    provider_url text,
    delete_supported boolean not null default false,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    completed_at timestamptz,
    deleted_at timestamptz
);

alter table public.social_publications
    add column if not exists integrity_version text,
    add column if not exists integrity_signature text;

create index if not exists idx_social_publications_guild_status
    on public.social_publications (guild_id, status, scheduled_at);

create index if not exists idx_social_publications_message_platform
    on public.social_publications (message_id, platform, action);

create index if not exists idx_social_publications_due_queue
    on public.social_publications (scheduled_at, created_at)
    where status = 'queued' and deleted_at is null;

drop trigger if exists social_publications_set_updated_at on public.social_publications;
create trigger social_publications_set_updated_at
before update on public.social_publications
for each row
execute function public.set_social_updated_at();

create table if not exists public.social_channel_routes (
    id uuid primary key default gen_random_uuid(),
    guild_id bigint not null,
    channel_id bigint,
    platform text not null,
    route_config jsonb not null default '{}'::jsonb,
    enabled boolean not null default true,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

alter table public.social_publications enable row level security;
alter table public.social_channel_routes enable row level security;

revoke all on public.social_publications from anon, authenticated;
revoke all on public.social_channel_routes from anon, authenticated;
revoke execute on function public.claim_due_social_publications(integer, bigint[]) from anon, authenticated;

create unique index if not exists uq_social_channel_routes_guild_default
    on public.social_channel_routes (guild_id, platform)
    where channel_id is null;

create unique index if not exists uq_social_channel_routes_channel
    on public.social_channel_routes (guild_id, channel_id, platform)
    where channel_id is not null;

drop trigger if exists social_channel_routes_set_updated_at on public.social_channel_routes;
create trigger social_channel_routes_set_updated_at
before update on public.social_channel_routes
for each row
execute function public.set_social_updated_at();

create or replace function public.claim_due_social_publications(
    claim_limit integer default 10,
    claim_guild_ids bigint[] default null
)
returns setof public.social_publications
language plpgsql
as $$
begin
    return query
    with due_rows as (
        select sp.publication_id
        from public.social_publications sp
        where sp.status = 'queued'
          and sp.deleted_at is null
          and sp.scheduled_at <= timezone('utc', now())
          and (sp.retry_after is null or sp.retry_after <= timezone('utc', now()))
          and (claim_guild_ids is null or sp.guild_id = any(claim_guild_ids))
        order by coalesce(sp.retry_after, sp.scheduled_at), sp.created_at
        for update skip locked
        limit greatest(claim_limit, 0)
    ),
    updated_rows as (
        update public.social_publications sp
        set status = 'processing',
            attempt_count = coalesce(sp.attempt_count, 0) + 1,
            retry_after = null,
            updated_at = timezone('utc', now())
        from due_rows
        where sp.publication_id = due_rows.publication_id
        returning sp.*
    )
    select * from updated_rows;
end;
$$;
