# Payments Runbook

This runbook is for operators responding to non-happy-path payment events in production.

Primary tools:

- `/payment-resolve <payment_id>` in Discord for on-chain reconciliation
- admin-chat tools `retry_payment(...)`, `hold_payment(...)`, and `release_payment(...)`
- `python scripts/check_payment_invariants.py` for daily read-only auditing

Important guardrails:

- `/payment-resolve` is the only intended reconciliation surface. There is no admin-chat LLM reconciliation tool.
- `retry_payment(...)` and `release_payment(...)` are already reconcile-gated before they mutate state.
- This plan does **not** ship a CI workflow or Railway cron config. Scheduler wiring is an operator decision; see the final section.

## Fast Triage

Start with the payment row:

```sql
select
  payment_id,
  guild_id,
  status,
  provider,
  producer,
  producer_ref,
  is_test,
  amount_token,
  amount_usd,
  tx_signature,
  send_phase,
  last_error,
  created_at,
  updated_at,
  submitted_at,
  completed_at
from payment_requests
where payment_id = '<PAYMENT_ID>';
```

Then classify:

- `status = manual_hold`: use the manual-hold playbook below.
- `status = failed`: decide whether to requeue or write off.
- `status = submitted` or `processing` for too long: treat as stuck; use `/payment-resolve` first.
- no admin DM but DB shows terminal state: use the DM-delivery playbook.

## Scenario: `manual_hold`

### Default Rule

Treat `manual_hold` as fail-closed. Do not retry blindly. Start with:

1. Run `/payment-resolve <payment_id>`.
2. Read the decision returned by the slash command.
3. Act based on the decision:
   - `reconciled_confirmed`: stop. The row is now corrected to `confirmed`.
   - `reconciled_failed`: stop. The row is now corrected to `failed`.
   - `allow_requeue`: only then consider retrying.
   - `keep_in_hold`: leave it held and escalate based on the reason.
   - `not_applicable`: the row is not in a reconcile-forward state; inspect manually.

### Decision Tree By `last_error` Prefix

Use the stored `last_error` as the first routing hint.

| `last_error` prefix | What it usually means | Operator action |
| --- | --- | --- |
| `rpc_unreachable:` | RPC transport was offline or unreachable while confirming. | Wait for RPC recovery, then run `/payment-resolve`. Do not requeue while the RPC path is still degraded. |
| `Confirmation timed out after submission` | The send reached `submitted`, but confirmation did not settle within the wait budget. | Run `/payment-resolve`. If it returns `allow_requeue`, then retry. Otherwise keep held. |
| `Ambiguous send error:` | Provider could not prove whether the send reached chain. | Run `/payment-resolve` first. If chain truth stays unclear, escalate; this is the classic double-send risk zone. |
| `Provider returned an invalid submitted-state result` | Provider contract was broken or incomplete. | Keep held, escalate to engineering. |
| `Submitted payment is missing a tx signature` | Local state is inconsistent. | Keep held, escalate. Do not retry until cause is understood. |
| `Recovery found submitted payment without tx signature` | Startup recovery found an impossible submitted row. | Keep held, escalate. |
| `Recovery could not determine submitted transaction status` | Recovery could not prove confirmed or failed. | Use `/payment-resolve`; if still inconclusive, leave held. |
| `Recovery found processing payment with stored tx signature` | A processing row already had a signature during recovery. | Treat as suspicious; use `/payment-resolve` before any retry. |
| `Recovery found ambiguous processing payment` | Restart occurred after an ambiguous send path. | Use `/payment-resolve`; if still unclear, escalate. |
| `Unsupported payment provider:` | Provider wiring mismatch or legacy row issue. | Do not retry until provider mapping is fixed. |
| `legacy provider could not be mapped:` | Startup migration could not infer `solana_grants` vs `solana_payouts`. | Fix the producer mapping first; then reassess the payment. |
| `cap check unavailable:` | Cap lookup could not derive a trustworthy USD value. | Resolve the pricing path or handle manually under operator approval. |
| `per-payment cap exceeded:` | Single payout is over the configured per-payment threshold. | Do not retry until the cap decision is intentionally changed. |
| `rolling daily cap exceeded:` | The 24h payout window is already over limit. | Wait or raise the cap intentionally, then re-request or retry. |
| `idempotency collision:` | Another active row already owns the same producer ref. | Investigate the sibling row before taking any action. |

### When To Escalate Immediately

Escalate to engineering without retrying when any of these are true:

- the row has no `tx_signature` but claims `submitted`
- the provider is unsupported or unknown
- `/payment-resolve` keeps returning `keep_in_hold` after RPC recovery
- the error mentions ambiguous send behavior and you cannot prove chain truth
- multiple active rows appear to represent the same payout intent

## Scenario: `failed`

`failed` means the subsystem believes the payment did not land successfully.

### Retry Only When All Of These Are True

- the business still wants the payout to happen
- `/payment-resolve <payment_id>` does **not** reconcile it to `confirmed`
- the failure reason is operational, not policy-related
- there is no cap breach blocking the retry

### Good Retry Candidates

- pre-submit failures
- transient RPC/provider failures that never produced a durable on-chain success
- `allow_requeue` from the reconciliation gate

### Bad Retry Candidates

- cap breaches
- unsupported provider or mapping issues
- ambiguous-send paths that still cannot be reconciled
- rows already corrected to `confirmed`

### How To Retry

Use the admin-chat tool:

```text
retry_payment(payment_id="<PAYMENT_ID>")
```

That path re-checks the stored signature before requeueing. If chain truth says the old send actually landed, the tool will reconcile instead of requeueing.

### When To Write Off

If the payout should not be retried, release the hold or preserve the failed state with an operator note:

```text
release_payment(payment_id="<PAYMENT_ID>", new_status="failed", reason="operator write-off after review")
```

Use a write-off when:

- the recipient no longer needs the payout
- the amount is below the threshold worth manual recovery
- the wallet is invalid and the user is unreachable
- the issue is policy-related rather than operational

## Scenario: Ghost-Verified Wallet

This means a wallet shows `verified_at`, but there is no qualifying confirmed test payment at or above the required floor.

The productionized daily audit detects this in `scripts/check_payment_invariants.py`.

### How To Clear And Re-Verify

1. Confirm the wallet really lacks a qualifying test payment.
2. Clear the verification marker.
3. Trigger the normal wallet verification flow so a fresh test payment is sent and confirmed.

Safe SQL pattern:

```sql
begin;

select
  wallet_id,
  guild_id,
  discord_user_id,
  wallet_address,
  verified_at
from wallet_registry
where wallet_id = '<WALLET_ID>'
for update;

update wallet_registry
set verified_at = null,
    updated_at = timezone('utc', now())
where wallet_id = '<WALLET_ID>';

commit;
```

Then ask the user to re-run the wallet verification flow. Do not mark `verified_at` manually unless you also have a confirmed qualifying test payment to back it.

## Scenario: Admin DM Not Received

The worker now tries DM first and falls back to `ADMIN_FALLBACK_CHANNEL_ID` when DM delivery is forbidden or rate-limited.

### Checklist

1. Check whether `ADMIN_USER_ID` is set correctly.
2. Check whether `ADMIN_FALLBACK_CHANNEL_ID` is configured and valid.
3. Look for logs containing:
   - `Delivered admin success alert`
   - `Delivered admin failed alert`
   - `admin alert undeliverable`
4. Check the fallback channel for the alert content.

### If Neither DM Nor Fallback Worked

- treat it as an observability failure, not payment-state truth
- inspect the payment row directly
- if terminal state is correct, no payment mutation is needed
- fix the Discord delivery path before the next incident

## Scenario: RPC Down / `rpc_unreachable`

`rpc_unreachable` is distinct from a normal confirmation timeout. It means the confirmation path could not reach the RPC transport successfully enough to prove chain status.

### Expected System Behavior

- submitted rows may move to `manual_hold` with `last_error = 'rpc_unreachable: confirmation RPC offline'`
- reconciliation will return `keep_in_hold` when chain status cannot be checked
- retry/release tools will refuse to push the row forward if chain truth is still unavailable

### Operator Playbook

1. Confirm the RPC outage is real.
2. Do **not** mass-retry held payments during the outage.
3. Wait for RPC recovery.
4. Run `/payment-resolve` on each held payment, or batch-review them carefully if you have multiple incidents.
5. Retry only the rows that resolve to `allow_requeue`.

### Degraded-Mode Expectation

New sends can still enter ambiguous territory if the transport is unstable. The correct response is backlog plus reconciliation, not optimistic replay.

## Scenario: Budget Cap Hit

Caps only apply where configured, currently `solana_payouts`.

### Symptoms

`last_error` will usually be one of:

- `cap check unavailable: token price missing`
- `per-payment cap exceeded: ...`
- `rolling daily cap exceeded: ...`

### Operator Questions

Before raising a cap, answer:

1. Is this a one-off operational exception or a real policy change?
2. How much has already been spent in the trailing 24h window?
3. Is the blocked payout legitimate and still desired?

### Quick Audit SQL

```sql
select
  coalesce(sum(amount_usd), 0) as rolling_24h_usd
from payment_requests
where status = 'confirmed'
  and provider = 'solana_payouts'
  and coalesce(completed_at, confirmed_at, updated_at, created_at) >= timezone('utc', now()) - interval '24 hours';
```

And the recent rows behind it:

```sql
select
  payment_id,
  guild_id,
  amount_usd,
  amount_token,
  provider,
  producer,
  completed_at,
  confirmed_at
from payment_requests
where status = 'confirmed'
  and provider = 'solana_payouts'
  and coalesce(completed_at, confirmed_at, updated_at, created_at) >= timezone('utc', now()) - interval '24 hours'
order by coalesce(completed_at, confirmed_at, updated_at, created_at) desc;
```

### When To Raise The Cap

Raise it only when:

- the payout is legitimate
- the 24h audit matches expectations
- the change is approved by the operator/owner responsible for real-money policy

If you raise the cap, record who approved it and why.

## Daily Invariant Audit

Run:

```bash
python scripts/check_payment_invariants.py
```

What it checks:

- every confirmed Solana row has a tx signature with `err = null` on chain
- no `pending_confirmation` or `processing` row is older than 24 hours
- every verified wallet has a qualifying confirmed test payment
- no Solana wallet address is reused across users
- the rolling 24h `solana_payouts` total is below the configured daily cap

Exit behavior:

- exit `0`: no hard invariant failures
- exit `1`: one or more hard failures, or the script itself could not complete successfully

## SQL Appendix From `audit_ghost_confirmed_payments.py`

These are the copy-paste SQL queries from the legacy ghost-audit script.

### Confirmed Solana Payments With Signatures

```sql
SELECT payment_id, guild_id, recipient_wallet, tx_signature,
       amount_token, is_test, confirmed_at, status
FROM payment_requests
WHERE status = 'confirmed'
  AND tx_signature IS NOT NULL
  AND chain = 'solana'
ORDER BY confirmed_at DESC NULLS LAST;
```

### Verified Solana Wallets

```sql
SELECT wallet_id, guild_id, discord_user_id, wallet_address,
       verified_at
FROM wallet_registry
WHERE chain = 'solana'
  AND verified_at IS NOT NULL;
```

### Ghost-Verified Wallet Join

```sql
SELECT DISTINCT ON (w.wallet_id)
    w.wallet_id, w.guild_id, w.discord_user_id,
    w.wallet_address, w.verified_at,
    pr.amount_token AS test_amount_token,
    pr.tx_signature AS test_tx_signature,
    pr.payment_id AS test_payment_id
FROM wallet_registry w
JOIN payment_requests pr
  ON pr.recipient_wallet = w.wallet_address
 AND pr.guild_id = w.guild_id
 AND pr.chain = 'solana'
 AND pr.is_test = true
WHERE w.chain = 'solana'
  AND w.verified_at IS NOT NULL
  AND pr.amount_token < 0.001
ORDER BY w.wallet_id, pr.confirmed_at DESC NULLS LAST;
```

## Scheduler Wire-Up

**OPERATOR DECISION:** this plan ships the audit script only. It does **not** ship a GitHub Actions workflow, a Railway cron config, or any external scheduler config.

Pick one of these deployment patterns:

### Option A: Railway Cron

Use this if the bot already runs on Railway and you want the audit close to the deployed environment.

- Pros: same secrets plane, simple operational ownership
- Cons: ties auditing to Railway scheduling and service configuration

Suggested command:

```bash
python scripts/check_payment_invariants.py
```

### Option B: GitHub Actions Scheduled Workflow

Use this if you want repository-visible runs and alerting history.

- Pros: visible run history, easy notifications
- Cons: requires storing production-grade read-only secrets in GitHub

Suggested command:

```bash
python scripts/check_payment_invariants.py
```

### Option C: External Scheduler

Use this if operations already has a standard cron runner, container scheduler, or incident platform.

- Pros: central ops ownership, independent from app deploys
- Cons: another integration surface to maintain

Suggested command:

```bash
python scripts/check_payment_invariants.py
```

### Recommended Minimum

Whichever scheduler you choose, ensure:

- it runs at least daily
- it uses read-only production credentials where possible
- non-zero exit codes page or alert somebody
- the raw JSON output is retained long enough to compare incidents across days
