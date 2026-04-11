# Arca Gidan Shared Supabase Cutover

This repository change adds the shared-schema side of the Arca Gidan migration. The live cutover still requires exported data from the old Arca Gidan project and dashboard-level Supabase configuration.

## What The New Migration Adds

- `ag_` prefixed competition, submission, vote, analytics, admin, and fraud-config tables
- `ag_profiles` and `ag_submission_details` AG-facing views
- `handle_new_user()` Discord OAuth bridge into `discord_members` + `ag_user_identities`
- `content_records` plus automatic syncing from `ag_submissions`
- `profile-pictures` bucket policies on the shared project

## Live Cutover Order

1. Apply `../supabase/migrations/20260322000000_merge_arca_gidan_into_shared_supabase.sql`.
2. Enable Discord OAuth on the shared Supabase project `ujlwuvkrxlvoswwkerdf`.
3. Add the shared Supabase callback URL to the Discord app:
   `https://ujlwuvkrxlvoswwkerdf.supabase.co/auth/v1/callback`
4. Export old Arca Gidan data from `rjrxtcfghwxqkzlgpcmb`:
   - `auth.users`
   - `competitions`
   - `profiles`
   - `submissions`
   - `votes`
   - `submission_analytics`
   - `vote_reviews`
   - `fraud_detection_config`
   - `admin_users`
   - `storage.objects` rows and bucket files for `profile-pictures`
5. Import historical auth users with their original UUIDs directly into `auth.users`.
6. Insert or update AG rows into the new `ag_` tables.
7. Copy `profile-pictures` objects into the shared bucket without changing their folder layout.
8. Leave the old AG project online as a read-only fallback.

## Auth Import Note

Historical `user_id` references in AG submissions, votes, analytics, and admin tables are UUID-based. To preserve those references, the import must keep the original `auth.users.id` values.

That means the historical auth import should use direct SQL into `auth.users`, not a create-user flow that generates fresh IDs.

After each imported auth row exists, link it to the Discord member bridge:

```sql
select ag_link_auth_identity(
  '<auth_user_uuid>'::uuid,
  '<discord_member_id>'::bigint,
  '<email@example.com>'
);
```

## Minimal Verification Queries

```sql
select count(*) from ag_competitions;
select count(*) from ag_submissions;
select count(*) from ag_votes;
select count(*) from ag_submission_analytics;

select count(*) from ag_user_identities;
select count(*) from ag_profiles;

select count(*) from content_records where source_app = 'arca-gidan';

select competition_id, count(*) from ag_public_vote_counts group by 1;
select * from ag_fraud_detection_summary;
```
