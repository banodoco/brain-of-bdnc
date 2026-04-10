create extension if not exists pgcrypto;

create or replace function public.set_payment_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := timezone('utc', now());
    return new;
end;
$$;

create table if not exists public.wallet_registry (
    wallet_id uuid primary key default gen_random_uuid(),
    guild_id bigint not null,
    discord_user_id bigint not null,
    chain text not null,
    wallet_address text not null,
    verified_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    check (char_length(btrim(chain)) > 0),
    check (char_length(btrim(wallet_address)) > 0)
);

create unique index if not exists uq_wallet_registry_guild_user_chain
    on public.wallet_registry (guild_id, discord_user_id, chain);

create unique index if not exists uq_wallet_registry_guild_chain_address
    on public.wallet_registry (guild_id, chain, wallet_address);

create index if not exists idx_wallet_registry_guild_verified
    on public.wallet_registry (guild_id, chain, verified_at desc nulls last);

drop trigger if exists wallet_registry_set_updated_at on public.wallet_registry;
create trigger wallet_registry_set_updated_at
before update on public.wallet_registry
for each row
execute function public.set_payment_updated_at();

create table if not exists public.payment_channel_routes (
    id uuid primary key default gen_random_uuid(),
    guild_id bigint not null,
    channel_id bigint,
    producer text not null,
    route_config jsonb not null default '{}'::jsonb,
    enabled boolean not null default true,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    check (char_length(btrim(producer)) > 0)
);

create unique index if not exists uq_payment_channel_routes_guild_default
    on public.payment_channel_routes (guild_id, producer)
    where channel_id is null;

create unique index if not exists uq_payment_channel_routes_channel
    on public.payment_channel_routes (guild_id, channel_id, producer)
    where channel_id is not null;

create index if not exists idx_payment_channel_routes_lookup
    on public.payment_channel_routes (guild_id, producer, enabled, channel_id);

drop trigger if exists payment_channel_routes_set_updated_at on public.payment_channel_routes;
create trigger payment_channel_routes_set_updated_at
before update on public.payment_channel_routes
for each row
execute function public.set_payment_updated_at();

create table if not exists public.payment_requests (
    payment_id uuid primary key default gen_random_uuid(),
    guild_id bigint not null,
    producer text not null,
    producer_ref text not null,
    wallet_id uuid references public.wallet_registry(wallet_id) on delete set null,
    recipient_discord_id bigint,
    recipient_wallet text not null,
    chain text not null,
    provider text not null,
    is_test boolean not null default false,
    route_key text,
    confirm_channel_id bigint not null,
    confirm_thread_id bigint,
    notify_channel_id bigint not null,
    notify_thread_id bigint,
    amount_token numeric(38, 18) not null check (amount_token > 0),
    amount_usd numeric(18, 8),
    token_price_usd numeric(18, 8),
    metadata jsonb not null default '{}'::jsonb,
    request_payload jsonb not null default '{}'::jsonb,
    status text not null default 'pending_confirmation'
        check (status in (
            'pending_confirmation',
            'queued',
            'processing',
            'submitted',
            'confirmed',
            'failed',
            'manual_hold',
            'cancelled'
        )),
    send_phase text
        check (send_phase is null or send_phase in ('pre_submit', 'submitted', 'ambiguous')),
    tx_signature text,
    attempt_count integer not null default 0 check (attempt_count >= 0),
    retry_after timestamptz,
    scheduled_at timestamptz not null default timezone('utc', now()),
    confirmed_by text,
    confirmed_by_user_id bigint,
    confirmed_at timestamptz,
    submitted_at timestamptz,
    completed_at timestamptz,
    last_error text,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    check (char_length(btrim(producer)) > 0),
    check (char_length(btrim(producer_ref)) > 0),
    check (char_length(btrim(recipient_wallet)) > 0),
    check (char_length(btrim(chain)) > 0),
    check (char_length(btrim(provider)) > 0),
    check (confirmed_by is null or char_length(btrim(confirmed_by)) > 0),
    check (not is_test or (amount_usd is null and token_price_usd is null))
);

create unique index if not exists uq_payment_requests_active_producer_ref
    on public.payment_requests (producer, producer_ref, is_test)
    where status not in ('failed', 'cancelled');

create unique index if not exists uq_payment_requests_tx_signature
    on public.payment_requests (tx_signature)
    where tx_signature is not null;

create index if not exists idx_payment_requests_guild_status
    on public.payment_requests (guild_id, status, scheduled_at);

create index if not exists idx_payment_requests_due_queue
    on public.payment_requests (scheduled_at, created_at)
    where status = 'queued';

create index if not exists idx_payment_requests_producer_ref
    on public.payment_requests (producer, producer_ref, is_test);

create index if not exists idx_payment_requests_wallet_status
    on public.payment_requests (wallet_id, status)
    where wallet_id is not null;

create index if not exists idx_payment_requests_pending_confirmation
    on public.payment_requests (guild_id, confirm_channel_id, confirm_thread_id, created_at)
    where status = 'pending_confirmation';

drop trigger if exists payment_requests_set_updated_at on public.payment_requests;
create trigger payment_requests_set_updated_at
before update on public.payment_requests
for each row
execute function public.set_payment_updated_at();

create or replace function public.claim_due_payment_requests(
    claim_limit integer default 10,
    claim_guild_ids bigint[] default null
)
returns setof public.payment_requests
language plpgsql
as $$
begin
    return query
    with due_rows as (
        select pr.payment_id
        from public.payment_requests pr
        where pr.status = 'queued'
          and pr.scheduled_at <= timezone('utc', now())
          and (pr.retry_after is null or pr.retry_after <= timezone('utc', now()))
          and (claim_guild_ids is null or pr.guild_id = any(claim_guild_ids))
        order by coalesce(pr.retry_after, pr.scheduled_at), pr.created_at
        for update skip locked
        limit greatest(claim_limit, 0)
    ),
    updated_rows as (
        update public.payment_requests pr
        set status = 'processing',
            attempt_count = coalesce(pr.attempt_count, 0) + 1,
            retry_after = null,
            updated_at = timezone('utc', now())
        from due_rows
        where pr.payment_id = due_rows.payment_id
        returning pr.*
    )
    select * from updated_rows;
end;
$$;

alter table public.wallet_registry enable row level security;
alter table public.payment_channel_routes enable row level security;
alter table public.payment_requests enable row level security;

revoke all on public.wallet_registry from anon, authenticated;
revoke all on public.payment_channel_routes from anon, authenticated;
revoke all on public.payment_requests from anon, authenticated;
revoke execute on function public.claim_due_payment_requests(integer, bigint[]) from anon, authenticated;
