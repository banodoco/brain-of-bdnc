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

create table if not exists public.admin_payment_intents (
    intent_id uuid primary key default gen_random_uuid(),
    guild_id bigint not null,
    channel_id bigint not null,
    admin_user_id bigint,
    recipient_user_id bigint not null,
    wallet_id uuid references public.wallet_registry(wallet_id) on delete set null,
    test_payment_id uuid references public.payment_requests(payment_id) on delete set null,
    final_payment_id uuid references public.payment_requests(payment_id) on delete set null,
    prompt_message_id bigint,
    receipt_prompt_message_id bigint,
    last_scanned_message_id bigint,
    resolved_by_message_id bigint,
    ambiguous_reply_count integer not null default 0,
    requested_amount_sol numeric(38, 18) not null check (requested_amount_sol > 0),
    producer_ref text not null,
    reason text,
    status text not null default 'awaiting_wallet'
        check (status in (
            'awaiting_wallet',
            'awaiting_test',
            'awaiting_test_receipt_confirmation',
            'awaiting_confirmation',
            'awaiting_admin_approval',
            'awaiting_admin_init',
            'manual_review',
            'confirmed',
            'completed',
            'failed',
            'cancelled'
        )),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    check (char_length(btrim(producer_ref)) > 0),
    check (reason is null or char_length(btrim(reason)) > 0)
);

alter table public.admin_payment_intents
    add column if not exists admin_user_id bigint;

alter table public.admin_payment_intents
    add column if not exists ambiguous_reply_count integer not null default 0;

alter table public.admin_payment_intents
    add column if not exists receipt_prompt_message_id bigint;

alter table public.admin_payment_intents
    drop constraint if exists admin_payment_intents_status_check;

alter table public.admin_payment_intents
    add constraint admin_payment_intents_status_check
    check (status in (
        'awaiting_wallet',
        'awaiting_test',
        'awaiting_test_receipt_confirmation',
        'awaiting_confirmation',
        'awaiting_admin_approval',
        'awaiting_admin_init',
        'manual_review',
        'confirmed',
        'completed',
        'failed',
        'cancelled'
    ));

create unique index if not exists uq_admin_payment_intents_active_recipient_channel
    on public.admin_payment_intents (guild_id, channel_id, recipient_user_id)
    where status not in ('completed', 'failed', 'cancelled');

create index if not exists idx_admin_payment_intents_guild_status
    on public.admin_payment_intents (guild_id, status);

create index if not exists idx_admin_payment_intents_guild_channel_status
    on public.admin_payment_intents (guild_id, channel_id, status);

drop trigger if exists admin_payment_intents_set_updated_at on public.admin_payment_intents;
create trigger admin_payment_intents_set_updated_at
before update on public.admin_payment_intents
for each row
execute function public.set_payment_updated_at();

alter table public.admin_payment_intents enable row level security;

revoke all on public.admin_payment_intents from anon, authenticated;
