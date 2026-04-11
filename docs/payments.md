# Payments Architecture

This document describes the current Solana payment subsystem after the worker/UI split, reconciliation hardening, and state-machine test pass. It is written for engineers who need to answer three questions quickly:

1. What states can a payment move through?
2. Who is allowed to move it?
3. Where do the safety invariants actually live?

## Scope

- Chains: Solana only.
- Providers: `solana_grants` and `solana_payouts`.
- Main entrypoints:
  - creation: `src/features/payments/payment_service.py:134-230`
  - confirmation authorization: `src/features/payments/payment_service.py:516-581`
  - execution and confirmation follow-up: `src/features/payments/payment_service.py:765-987`
  - worker loop: `src/features/payments/payment_worker_cog.py:92-140`
  - UI confirmation and `/payment-resolve`: `src/features/payments/payment_ui_cog.py:19-190`
  - DB transition and reconciliation helpers: `src/common/db_handler.py:1422-1456`, `src/common/db_handler.py:2218-2315`
  - queue claiming and active uniqueness rules: `sql/payments.sql:134-198`

## High-Level Shape

`main.py` wires two separate Solana providers into one `PaymentService`: `solana_grants` and `solana_payouts`, then mounts `PaymentWorkerCog` and `PaymentUICog` separately. See `main.py:172-189` and `main.py:262-266`.

That split matters:

- grants traffic is sent through the grants wallet
- admin-chat payouts are sent through the payouts wallet
- only `solana_payouts` is cap-limited by default in the current boot wiring (`main.py:185-199`)

## State Machine

The intended payment lifecycle is:

```text
create
  |
  v
pending_confirmation
  |
  | confirm_payment(...)
  v
queued
  |
  | claim_due_payment_requests()  [FOR UPDATE SKIP LOCKED]
  v
processing
  |
  | execute_payment(...)
  v
submitted
  |\
  | \ confirm / check_status
  |  \
  |   +---------------------> confirmed
  |   +---------------------> failed
  |   +---------------------> manual_hold
  |
  +-------------------------> cancelled

manual interventions
  failed ------requeue------> queued
  manual_hold --release-----> failed

reconciliation edges
  submitted ----\
  processing ----+--> confirmed   via force_reconcile_payment_to_confirmed(...)
  failed --------/
  manual_hold ---/

  submitted ----\
  processing ----+--> failed      via force_reconcile_payment_to_failed(...)
  failed --------/
  manual_hold ---/
```

Where this is enforced:

- queue claim atomicity: `sql/payments.sql:166-198`
- confirmation to `queued`: `src/features/payments/payment_service.py:550-581`
- execution to `submitted` or terminal states: `src/features/payments/payment_service.py:765-836`
- follow-up confirmation reconciliation from `submitted`: `src/features/payments/payment_service.py:945-987`
- startup recovery of stale `processing` and `submitted` rows: `src/features/payments/payment_service.py:838-935`
- operator reconciliation path: `src/features/payments/payment_service.py:583-701`
- force-reconcile DB helpers: `src/common/db_handler.py:2218-2315`

### Why the Worker Race Is Controlled

Two different protections work together:

- Only `queued` rows are claimable by the worker RPC, and claiming is an atomic `queued -> processing` transition under `FOR UPDATE SKIP LOCKED`: `sql/payments.sql:166-198`.
- Recovery only revisits `processing` and `submitted` rows, and the startup overlap pass now applies a reclaim-age guard before it takes ownership of recent rows: `src/features/payments/payment_service.py:838-935`.

## Authorization Model

`PaymentActorKind` is defined in `src/features/payments/payment_service.py:26-30`. Producer-level authorization lives in `src/features/payments/producer_flows.py:16-30`.

Current matrix:

| Producer | Test Confirmers | Real Confirmers | Notes |
| --- | --- | --- | --- |
| `grants` | `AUTO` | `RECIPIENT_CLICK` | Test payments auto-confirm after wallet verification flow; real grant payouts require recipient button confirmation. |
| `admin_chat` | `AUTO` | `RECIPIENT_CLICK`, `RECIPIENT_MESSAGE` | Real admin payouts can be confirmed by the recipient via button or message path. |

Notes:

- `AUTO` is the test-payment confirmer used by producer policy, not a bypass around producer policy. See `src/features/payments/payment_service.py:516-548` and `src/features/payments/producer_flows.py:16-30`.
- `ADMIN_DM` exists as an actor kind in `src/features/payments/payment_service.py:26-30`, but no current producer flow grants it confirmation authority yet. If the admin-DM approval megaplan lands later, this table must be updated from `producer_flows.py`.
- Authorization is fail-closed. If a producer is unknown or the actor kind is not listed for that producer and payment class, confirmation is rejected by `_authorize_actor(...)`: `src/features/payments/payment_service.py:516-548`.

## Creation, Idempotency, And Collision Rules

`request_payment(...)` is now a thin orchestrator that delegates to named helpers: `src/features/payments/payment_service.py:134-230`.

Important subcontracts:

- input normalization and fail-fast validation: `src/features/payments/payment_service.py:232-276`
- collision detection and idempotent rereads: `src/features/payments/payment_service.py:278-306`
- amount derivation: `src/features/payments/payment_service.py:308-354`
- cap enforcement: `src/features/payments/payment_service.py:356-406`
- final row persistence plus duplicate reread fallback: `src/features/payments/payment_service.py:408-514`

The active uniqueness rule is implemented twice on purpose:

- database backstop: `sql/payments.sql:134-136`
- application classification of collision versus idempotent replay: `src/features/payments/payment_service.py:278-306`

Non-obvious detail: the unique index explicitly excludes `failed` and `cancelled`, so a terminal write-off does not permanently block a legitimate later retry with the same `(producer, producer_ref, is_test)` tuple. See `sql/payments.sql:134-136`.

The admin-chat producer-ref format now uses millisecond precision to avoid same-second collisions for the same recipient: `src/features/admin_chat/tools.py:2542-2554`.

## Cap Enforcement Model

Cap logic lives in `_enforce_caps(...)`: `src/features/payments/payment_service.py:356-406`.

Inputs:

- `per_payment_usd_cap`
- `daily_usd_cap`
- `capped_providers`

Behavior:

- caps are evaluated only for providers listed in `capped_providers`
- the per-payment cap rejects a single oversized request immediately
- the daily cap uses recent confirmed spend from the DB to reject new requests once the rolling limit is exceeded
- non-capped providers bypass this branch entirely

Current production wiring in `main.py` applies caps to `solana_payouts` only: `main.py:185-199`.

## Fail-Closed Contract

The system is intentionally biased toward `manual_hold`, rejection, or no-op reread instead of optimistic progression.

Core invariants and where they live:

1. A payment cannot be created without a wallet, producer, producer ref, and chain.
   - `src/features/payments/payment_service.py:232-276`

2. A recipient cannot confirm a payment unless the producer flow explicitly allows that actor kind.
   - `src/features/payments/payment_service.py:516-548`
   - `src/features/payments/producer_flows.py:16-30`

3. Claiming work from the queue is atomic and only touches `queued` rows.
   - `sql/payments.sql:166-198`

4. A payment that hits execution-time uncertainty is held rather than silently advanced.
   - worker exception guard: `src/features/payments/payment_worker_cog.py:121-140`
   - execution-time provider/recipient/manual-hold branches: `src/features/payments/payment_service.py:765-836`
   - post-submit confirmation uncertainty: `src/features/payments/payment_service.py:945-987`

5. RPC transport failure is distinguished from a responsive confirmation timeout.
   - `rpc_unreachable` handling in recovery and post-submit confirm: `src/features/payments/payment_service.py:838-935`, `src/features/payments/payment_service.py:945-987`

6. Test payments never persist USD-derived amounts.
   - schema check: `sql/payments.sql:131`
   - request amount derivation path: `src/features/payments/payment_service.py:308-354`

7. Confirmed rows must be backed by a tx signature history trail when status-changing DB helpers run.
   - append helper: `src/common/db_handler.py:1422-1456`
   - terminal transition helpers: `src/common/db_handler.py:2151-2315`

8. Recovery and reconciliation never silently “imagine” success. They check provider status or keep the payment blocked.
   - `src/features/payments/payment_service.py:583-701`
   - `src/features/payments/payment_service.py:838-935`

## Reconciliation Contract

Reconciliation exists because on-chain truth can be authoritative even when local state is stale or ambiguous.

The normal transition helpers remain strict:

- `mark_payment_confirmed(...)`: `src/common/db_handler.py:2151-2168`
- `mark_payment_failed(...)`: `src/common/db_handler.py:2195-2216`

The reconciliation helpers intentionally bypass those stricter starting-state guards:

- `_force_reconcile_payment_status(...)`: `src/common/db_handler.py:2218-2280`
- `force_reconcile_payment_to_confirmed(...)`: `src/common/db_handler.py:2282-2298`
- `force_reconcile_payment_to_failed(...)`: `src/common/db_handler.py:2300-2315`

That bypass is deliberate. Reconciliation is not “normal flow”; it is a history-correction path driven by authoritative on-chain status. Accountability comes from:

- explicit allowed starting states
- tx-signature history append
- warning-level logging on forced status correction

Operator reachability is intentionally narrow:

- service decision logic: `src/features/payments/payment_service.py:583-701`
- slash command entrypoint: `src/features/payments/payment_ui_cog.py:159-190`

There is no admin-chat LLM reconciliation tool. `/payment-resolve` is the intended operator surface, and it gates on `ADMIN_USER_ID` inside the callback body: `src/features/payments/payment_ui_cog.py:164-190`.

## Worker And UI Responsibilities

`PaymentWorkerCog` owns background execution:

- startup recovery: `src/features/payments/payment_worker_cog.py:86-90`, `src/features/payments/payment_worker_cog.py:110-120`
- recurring queue claim loop: `src/features/payments/payment_worker_cog.py:92-105`
- unexpected-error fail-closed hold: `src/features/payments/payment_worker_cog.py:121-140`

`PaymentUICog` owns user/operator-facing interaction:

- persistent recipient confirmation view: `src/features/payments/payment_ui_cog.py:19-93`
- pending confirmation view re-registration: `src/features/payments/payment_ui_cog.py:110-130`
- slash-based reconciliation: `src/features/payments/payment_ui_cog.py:159-190`

This split keeps execution logic off the UI path and makes startup recovery independent of Discord message handling.

## Non-Obvious Constraints

These are the gremlin-prone details a new engineer is most likely to miss.

### Rent-Exempt Test Amount Floor

`PaymentService` enforces a minimum test payment size of `0.002 SOL` by checking lamports during construction:

- constants: `src/features/payments/payment_service.py:19-20`
- constructor guard: `src/features/payments/payment_service.py:101-123`

This is intentionally constructor-time, not bot-startup-only, so every construction path gets the same guard.

### Static Floor Plus Dynamic Priority Fees

The shared Solana client uses:

- a static fee floor and ceiling from env: `src/features/grants/solana_client.py:70-78`
- dynamic 75th-percentile fee selection with fallback to the floor: `src/features/grants/solana_client.py:91-123`
- the chosen fee on every send: `src/features/grants/solana_client.py:169-183`

This avoids both zero-fee optimism during congestion and runaway fee spikes.

### Two-Wallet Split

The process boots separate grants and payouts clients/providers and injects them into a single service:

- provider wiring: `main.py:172-189`
- grants requests use `solana_grants`: `src/features/grants/grants_cog.py:583`, `src/features/grants/grants_cog.py:697`
- admin-chat requests use `solana_payouts`: `src/features/admin_chat/admin_chat_cog.py:252`, `src/features/admin_chat/admin_chat_cog.py:370`

### Idempotency Index Excludes Failed And Cancelled

The DB unique index only protects active rows:

- `sql/payments.sql:134-136`

The service mirrors that behavior when classifying collision versus replay:

- `src/features/payments/payment_service.py:278-306`

This keeps legitimate retries possible without relaxing protection for active rows.

### Millisecond Producer Refs For Admin-Initiated Payments

Admin-initiated payouts use millisecond precision in `producer_ref`:

- `src/features/admin_chat/tools.py:2542-2554`

That fix exists specifically to prevent two operators paying the same recipient in the same second from collapsing onto one logical request.

### Tx Signature History Is Part Of The Audit Story

The schema stores `tx_signature_history` and backfills legacy rows:

- schema column and backfill: `sql/payments.sql:209-220`
- append helper: `src/common/db_handler.py:1422-1456`

Reconciliation and normal terminal transitions both append history instead of replacing it.

## Practical Reading Order

If you are new to this subsystem, read in this order:

1. `src/features/payments/payment_service.py:134-230`
2. `src/features/payments/payment_service.py:516-701`
3. `src/features/payments/payment_service.py:765-987`
4. `src/features/payments/payment_worker_cog.py:86-140`
5. `src/common/db_handler.py:2151-2315`
6. `sql/payments.sql:134-198`

That sequence covers creation, authorization, execution, recovery, reconciliation, and the DB backstops without making you reverse-engineer the entire repo first.
